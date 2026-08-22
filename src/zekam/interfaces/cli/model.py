"""`zekam model` komutlari: envanter, health, sozlesme ve rapor.

Hicbir komut ham endpoint adresi veya credential degeri yazdirmaz. Health probe
gercek bir saglayiciya baglanmadan once exact authorization, claim/receipt ve Secret
Broker gerektirir. Production CLI sentetik probe ile health basarisi uretemez.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer
from rich.console import Console
from rich.table import Table

from zekam.application.environment import environment_value
from zekam.application.execution import ExecutionHost
from zekam.application.governance import DEFAULT_POLICY_NAME, GovernanceService
from zekam.application.model_benchmark_service import (
    BenchmarkExecutionService,
    DeterministicLocalBenchmarkAdapter,
    LocalProcessBenchmarkAdapter,
    LocalProcessBenchmarkVerifier,
    RuntimeBenchmarkClaimGateway,
    default_fixture_file,
    load_fixture_registry,
)
from zekam.application.model_decision_service import ModelDecisionService
from zekam.application.model_health_service import (
    AuthorizationRequiredProviderProbe,
    ModelHealthService,
)
from zekam.application.model_registry import (
    ImportReport,
    load_inventory,
    summarize_snapshot,
    verify_snapshot,
)
from zekam.application.model_report import build_report
from zekam.application.opencode_embedding import (
    OPENCODE_EMBEDDING_SECRET_REF_NAME,
    OpenCodeCredentialStore,
    OpenCodeEndpointResolver,
    build_opencode_embedding_probe_manifest,
    default_opencode_config_file,
    evaluate_opencode_aihub_models,
    evaluate_opencode_embedding_response,
    load_opencode_aihub_catalog,
    load_opencode_embedding_configuration,
)
from zekam.application.provider_adapter import (
    AuthorizedProviderClient,
    EnvironmentEndpointResolver,
    UrllibJsonProviderTransport,
    UrllibMultipartProviderTransport,
)
from zekam.application.provider_configuration import (
    evaluate_provider_configuration,
    load_provider_bindings,
)
from zekam.application.provider_contract_execution import (
    ProviderExecutionManifest,
    assemble_contract_observations,
    build_provider_execution_manifest,
    build_provider_policy_candidate,
    evaluate_text_contracts,
    load_provider_contract_fixtures,
    prepare_provider_contract_calls,
)
from zekam.application.provider_contract_runner import (
    RuntimeProviderContractRunner,
    verify_exact_provider_authorization,
)
from zekam.application.realm_context import RealmContext
from zekam.application.secret_broker import EnvironmentSecretStore, SecretBroker
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import Checkpoint
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.model_benchmark import (
    BenchmarkPlan,
    BenchmarkSuite,
    DecisionRequirements,
    SuiteKind,
    VerifierIdentity,
    build_project_suite,
)
from zekam.domain.model_contract import evaluate_observation
from zekam.domain.model_inventory import Modality
from zekam.domain.policy import PolicyDocument, default_policy_rules
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, FailureCategory, Job, JobKind
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    DataClassification,
    SecretBackend,
    SecretRef,
)
from zekam.domain.work import EffectKind, PlanStep
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.model_benchmark_repository import BenchmarkRepository
from zekam.infrastructure.postgres.model_repository import (
    HealthReportRepository,
    ModelInventoryRepository,
)
from zekam.infrastructure.postgres.security_repository import (
    AuthorizationRepository,
    SecretRefRepository,
)
from zekam.interfaces.cli import model_campaign as model_campaign_commands
from zekam.interfaces.cli import model_routing as model_routing_commands
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail, fail_from

app = typer.Typer(name="model", help="Model envanteri ve saglik islemleri", no_args_is_help=True)
app.add_typer(model_campaign_commands.app)
app.add_typer(model_routing_commands.app)
app.command("resolve")(model_campaign_commands.resolve_command)
console = Console()

_PROVIDER_LIVE_SOURCE_FILES = (
    "config/model_provider_bindings.yaml",
    "config/model_provider_contract_fixtures.yaml",
    "src/zekam/application/provider_adapter.py",
    "src/zekam/application/provider_configuration.py",
    "src/zekam/application/provider_contract_execution.py",
    "src/zekam/application/provider_contract_runner.py",
    "src/zekam/application/execution.py",
    "src/zekam/application/governance.py",
    "src/zekam/application/model_registry.py",
    "src/zekam/application/opencode_embedding.py",
    "src/zekam/application/secret_broker.py",
    "src/zekam/domain/canonical.py",
    "src/zekam/domain/context_continuity.py",
    "src/zekam/domain/model_contract.py",
    "src/zekam/domain/policy.py",
    "src/zekam/domain/resources.py",
    "src/zekam/domain/runtime.py",
    "src/zekam/domain/security.py",
    "src/zekam/domain/work.py",
    "src/zekam/infrastructure/postgres/runtime_repository.py",
    "src/zekam/infrastructure/postgres/context_continuity_repository.py",
    "src/zekam/infrastructure/postgres/security_repository.py",
    "src/zekam/interfaces/cli/model.py",
    "modeller/KANONIK_MODEL_ENVANTERI.yaml",
)

_OPENCODE_EMBEDDING_CANDIDATES = (
    "openai/Qwen/Qwen3-Embedding-0.6B",
    "openai/BAAI/bge-m3",
    "intfloat/e5-mistral-7b-instruct",
)
_DEFAULT_OPENCODE_CONFIG_FILE = default_opencode_config_file()


def _provider_source_revision() -> tuple[str, str]:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        check=False,
    )
    head = completed.stdout.strip()
    if completed.returncode != 0 or len(head) != 40:
        raise PolicyViolation("Provider live source HEAD okunamadi")
    root = Path(__file__).resolve().parents[4]
    file_digests: dict[str, str] = {}
    for relative in _PROVIDER_LIVE_SOURCE_FILES:
        path = root / relative
        if not path.is_file():
            raise PolicyViolation("Provider live source dosya seti eksik")
        file_digests[relative] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return head, f"{head}:{digest(file_digests)}"


def _provider_plan_steps(manifest: ProviderExecutionManifest) -> tuple[PlanStep, ...]:
    calls = manifest.calls
    orchestration_id = "provider-live-contracts"
    steps: list[PlanStep] = [
        PlanStep(
            step_id=orchestration_id,
            title=f"Exact 10 provider contract orchestration {manifest.manifest_digest}",
            effect=EffectKind.NONE,
            logical_resources=manifest.policy_resources,
            risk="critical",
        )
    ]
    previous: str | None = orchestration_id
    for call in calls:
        steps.append(
            PlanStep(
                step_id=call.call_id,
                title=(f"Exact provider contract {call.call_id} {call.authorization_plan_digest}"),
                effect=EffectKind.PROVIDER_CALL,
                logical_resources=(call.target, call.call_resource),
                depends_on=() if previous is None else (previous,),
                risk="critical",
            )
        )
        previous = call.call_id
    return tuple(steps)


def _embedding_probe_steps(manifest: ProviderExecutionManifest) -> tuple[PlanStep, ...]:
    orchestration_id = "opencode-embedding-probe"
    steps: list[PlanStep] = [
        PlanStep(
            step_id=orchestration_id,
            title=f"OpenCode embedding aday siralamasi {manifest.manifest_digest}",
            effect=EffectKind.NONE,
            logical_resources=manifest.policy_resources,
            risk="critical",
        )
    ]
    previous = orchestration_id
    for call in manifest.calls:
        steps.append(
            PlanStep(
                step_id=call.call_id,
                title=f"Public sentetik embedding probe {call.call_id}",
                effect=EffectKind.PROVIDER_CALL,
                logical_resources=(call.target, call.call_resource),
                depends_on=(previous,),
                risk="critical",
            )
        )
        previous = call.call_id
    return tuple(steps)


@app.command("opencode-status")
def opencode_status_command(
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="OpenCode config absolute yolu"),
    ] = None,
    require_ready: Annotated[
        bool,
        typer.Option(
            "--hazir-olmasini-iste",
            help="Ambiguous/absent/unhealthy configured model varsa fail-closed doner",
        ),
    ] = False,
    output_json: Annotated[bool, typer.Option("--json", help="Sanitize JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """OpenCode AIHub ailesi ve canonical health eligibility kesitini raporlar."""

    del output_json
    try:
        inventory = load_inventory()
        catalog = load_opencode_aihub_catalog(
            config_file or _DEFAULT_OPENCODE_CONFIG_FILE,
            provider_id="litellm",
        )
        with RealmSession(home, realm) as realm_context:
            fresh_ids = tuple(
                item.model_id for item in _service(realm_context).benchmark_eligible()
            )
        evaluations = evaluate_opencode_aihub_models(
            catalog,
            inventory,
            fresh_benchmark_eligible_ids=fresh_ids,
        )
        configured = tuple(item for item in evaluations if item.configured)
        ready = len(configured) == len(catalog.configured_model_ids) and all(
            item.canonical_present and item.benchmark_eligible for item in configured
        )
        document = {
            "schema": "zekam-opencode-aihub-status/v1",
            "provider": catalog.sanitized(),
            "configured_model_count": len(catalog.configured_model_ids),
            "canonical_matched_count": sum(item.canonical_present for item in configured),
            "active_count": sum(item.active for item in configured),
            "enabled_count": sum(item.enabled for item in configured),
            "benchmark_eligible_count": sum(item.benchmark_eligible for item in configured),
            "ready": ready,
            "models": [item.sanitized() for item in evaluations],
            "provider_calls_made": 0,
            "network_calls_made": 0,
            "endpoint_values_reported": 0,
            "secret_values_reported": 0,
            "grants_authority": False,
        }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    if require_ready and not document["ready"]:
        raise fail("OpenCode AIHub configured model eligibility hazir degil", 6)


@app.command("opencode-embedding-probe")
def opencode_embedding_probe_command(
    project_uuid: Annotated[UUID, typer.Option("--project-uuid", help="Kanonik project UUID")],
    work_id: Annotated[UUID, typer.Option("--work", help="Kanonik Work Item UUID")],
    actor_id: Annotated[UUID, typer.Option("--actor", help="Yetkili kanonik actor UUID")],
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="OpenCode config absolute yolu"),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--uygula", help="Uc exact sentetik embedding probe cagrisi yapar"),
    ] = False,
    output_json: Annotated[bool, typer.Option("--json", help="Sanitize JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """OpenCode embedding adaylarini public sentetik veriyle exact olarak siralar."""

    del output_json
    if not apply:
        raise fail(
            "OpenCode embedding probe exact --project-uuid/--work/--actor/--uygula ister", 64
        )
    try:
        inventory = load_inventory()
        configurations = tuple(
            load_opencode_embedding_configuration(
                config_file or _DEFAULT_OPENCODE_CONFIG_FILE,
                provider_id="litellm",
                selected_model_id=model_id,
                inventory=inventory,
            )
            for model_id in _OPENCODE_EMBEDDING_CANDIDATES
        )
        if len(configurations) != 3:
            raise PolicyViolation("OpenCode embedding probe exact uc aday ister")
        credential_locator = configurations[0].credential_locator
        if not (environment_value(os.environ, credential_locator, strip=True) or ""):
            raise PolicyViolation("OpenCode embedding credential locator hazir degil")
        manifest, prepared = build_opencode_embedding_probe_manifest(configurations)
        source_head, source_revision = _provider_source_revision()
        with RealmSession(home, realm) as realm_context:
            governance = GovernanceService(
                realm_context.connection, realm_context.realm, actor_id=actor_id
            )
            graph = WorkGraphService(
                realm_context.connection, realm_context.realm, actor_id=actor_id
            )
            work_item = graph.items.get(work_id)
            if work_item.project_id != project_uuid:
                raise PolicyViolation("OpenCode embedding Work/project binding mismatch")
            base_policy = governance.policies.current(DEFAULT_POLICY_NAME)
            if base_policy is None:
                raise PolicyViolation("Kanonik varsayilan policy bulunamadi")
            candidate_policy = build_provider_policy_candidate(base_policy, manifest)
            secret_repository = SecretRefRepository(
                realm_context.connection, realm_context.realm_id
            )
            secret_ref = secret_repository.current_by_name(OPENCODE_EMBEDDING_SECRET_REF_NAME)
            authorizations: dict[str, Authorization] = {}
            with realm_context.connection.transaction():
                if secret_ref is None:
                    secret_ref = SecretRef.create(
                        realm_id=realm_context.realm_id,
                        name=OPENCODE_EMBEDDING_SECRET_REF_NAME,
                        provider=configurations[0].provider_id,
                        purpose="public synthetic embedding candidate probe",
                        allowed_operations=("embeddings",),
                        store_backend=SecretBackend.ENVIRONMENT,
                        store_locator=credential_locator,
                    )
                    secret_repository.add(secret_ref)
                if (
                    secret_ref.provider != configurations[0].provider_id
                    or secret_ref.store_backend is not SecretBackend.ENVIRONMENT
                    or secret_ref.store_locator != credential_locator
                    or not secret_ref.permits("embeddings")
                    or not secret_ref.is_usable()
                ):
                    raise PolicyViolation("OpenCode embedding SecretRef exact metadata drift")
                governance.policies.append(candidate_policy)
                task_plan = graph.create_plan(
                    work_id,
                    source_revision=source_revision,
                    policy_digest=candidate_policy.policy_digest,
                    steps=_embedding_probe_steps(manifest),
                )
                authorization_repository = AuthorizationRepository(
                    realm_context.connection, realm_context.realm_id
                )
                for item in prepared:
                    authorization = Authorization.issue(
                        realm_id=realm_context.realm_id,
                        actor_id=actor_id,
                        work_item_id=work_id,
                        plan_id=task_plan.id,
                        plan_digest=item.plan.authorization_plan_digest,
                        effect_digest=item.plan.effect_request.effect_digest,
                        scope=AuthorizationScope(
                            allowed_resources=(item.plan.target, item.plan.call_resource),
                            allowed_effects=(EffectKind.PROVIDER_CALL.value,),
                            provider_refs=(item.plan.provider_ref,),
                            secret_ref_ids=(secret_ref.id,),
                            data_classifications=(DataClassification.PUBLIC,),
                        ),
                        risk="critical",
                        lifetime=dt.timedelta(minutes=15),
                    )
                    authorization_repository.issue(authorization)
                    authorizations[item.plan.call_id] = authorization

            endpoint_resolver = OpenCodeEndpointResolver(
                provider_id=configurations[0].provider_id,
                endpoint_ref=prepared[0].plan.endpoint_ref,
                endpoint=configurations[0].embedding_endpoint,
            )
            client = AuthorizedProviderClient(
                governance,
                endpoint_resolver,
                SecretBroker(
                    {
                        SecretBackend.ENVIRONMENT: OpenCodeCredentialStore(
                            provider_id=configurations[0].provider_id,
                            credential_locator=credential_locator,
                        )
                    }
                ),
                UrllibJsonProviderTransport(),
            )
            capability = f"provider.embedding.probe.{manifest.manifest_digest[-16:]}"
            host = ExecutionHost(
                realm_context.connection,
                realm_context.realm_id,
                worker_label="opencode-embedding-probe",
            )
            job, _ = host.jobs.enqueue(
                Job.create(
                    realm_id=realm_context.realm_id,
                    project_id=project_uuid,
                    kind=JobKind.PROVIDER_CALL,
                    idempotency_key=f"opencode-embedding-probe:{manifest.manifest_digest}",
                    resources=parse_requests(
                        write=tuple(item.plan.call_resource for item in prepared)
                    ),
                    required_capabilities=(capability,),
                    max_attempts=1,
                    work_item_id=work_id,
                    plan_id=task_plan.id,
                    step_id="opencode-embedding-probe",
                )
            )
            claimed = host.acquire_work(capabilities=(capability,))
            if claimed is None or claimed.job.id != job.id:
                raise PolicyViolation("OpenCode embedding runtime job claim edilemedi")
            runner = RuntimeProviderContractRunner(host=host, work=claimed, client=client)
            executions = []
            metrics = []
            try:
                for configuration, item in zip(configurations, prepared, strict=True):
                    started = time.perf_counter()
                    execution = runner.invoke(
                        item,
                        secret_ref=secret_ref,
                        authorization=authorizations[item.plan.call_id],
                        consumed_by=f"cli:opencode-embedding-probe:{item.plan.call_id}",
                    )
                    elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
                    executions.append(execution)
                    metrics.append(
                        evaluate_opencode_embedding_response(
                            configuration,
                            execution.provider_result.response,
                            latency_ms=elapsed_ms,
                        )
                    )
            except Exception:
                for item in prepared:
                    authorization = authorizations[item.plan.call_id]
                    governance.revoke_authorization(
                        authorization.id, "embedding-probe-aborted-no-retry"
                    )
                raise

            ranked = sorted(
                (item for item in metrics if item.verified),
                key=lambda item: (-item.semantic_margin, item.latency_ms, item.model_id),
            )
            succeeded = bool(ranked)
            run_digest = digest(
                {
                    "manifest_digest": manifest.manifest_digest,
                    "receipts": [str(item.receipt.id) for item in executions],
                    "metric_digests": [item.evidence_digest for item in metrics],
                    "ranking": [item.model_id for item in ranked],
                }
            )
            if succeeded:
                plan_step_ids = tuple(step.step_id for step in task_plan.steps)
                evidence_by_call = {
                    item.call_id: item.receipt.adapter_evidence_digest for item in executions
                }
                if any(value is None for value in evidence_by_call.values()):
                    raise PolicyViolation("Embedding probe completed receipt evidence ister")
                checkpoint = Checkpoint(
                    checkpoint_id=f"opencode-embedding-probe-{job.id}",
                    project_id=str(project_uuid),
                    work_item_id=str(work_id),
                    plan_revision_id=str(task_plan.id),
                    source_revision=source_revision,
                    plan_steps=plan_step_ids,
                    completed_steps=plan_step_ids,
                    pending_steps=(),
                    step_results=tuple(
                        (
                            step_id,
                            run_digest
                            if step_id == "opencode-embedding-probe"
                            else str(evidence_by_call[step_id]),
                        )
                        for step_id in plan_step_ids
                    ),
                    context_manifest_digest=manifest.manifest_digest,
                    journal_head_digest=run_digest,
                    next_safe_action="independent-retrieval-verification",
                    created_at=dt.datetime.now(dt.UTC),
                )
                ContextContinuityRepository(
                    realm_context.connection,
                    realm_context.realm_id,
                    project_uuid,
                    work_id,
                ).store_checkpoint(checkpoint, task_plan_id=task_plan.id, job_id=job.id)
            host.finish(
                claimed,
                outcome=AttemptOutcome.SUCCEEDED if succeeded else AttemptOutcome.FAILED,
                result_digest=run_digest if succeeded else None,
                failure_category=None if succeeded else FailureCategory.VALIDATION,
            )
            document = {
                "schema": "zekam-opencode-embedding-probe/v1",
                "status": "passed" if succeeded else "failed",
                "project_id": str(project_uuid),
                "work_id": str(work_id),
                "task_plan_id": str(task_plan.id),
                "source_head": source_head,
                "source_revision": source_revision,
                "manifest_digest": manifest.manifest_digest,
                "fixture_digest": manifest.fixture_digest,
                "provider_calls_made": len(executions),
                "network_calls_made": len(executions),
                "data_classification": "public",
                "project_source_sent": False,
                "ranking": [item.model_id for item in ranked],
                "metrics": [item.sanitized() for item in metrics],
                "calls": [
                    {
                        "call_id": item.call_id,
                        "authorization_id": str(item.provider_result.authorization_id),
                        "claim_id": str(item.claim.id),
                        "receipt_id": str(item.receipt.id),
                        "receipt_status": item.receipt.status.value,
                        "response_digest": item.provider_result.response_digest,
                    }
                    for item in executions
                ],
                "run_digest": run_digest,
                "grants_authority": False,
            }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    if document["status"] != "passed":
        raise fail("OpenCode embedding adaylarindan hicbiri contract gecemedi", 7)


@app.command("benchmark")
def benchmark_command(
    model: Annotated[str | None, typer.Option("--model", help="Benchmark Model ID")] = None,
    project: Annotated[
        str | None, typer.Option("--project", help="Project-specific micro suite proje ID")
    ] = None,
    capability_digest: Annotated[
        str | None, typer.Option("--capability-digest", help="Capability profile SHA-256")
    ] = None,
    inventory_digest: Annotated[
        str | None, typer.Option("--inventory-digest", help="Inventory SHA-256")
    ] = None,
    policy_digest: Annotated[
        str | None, typer.Option("--policy-digest", help="Policy SHA-256")
    ] = None,
    repetitions: Annotated[int, typer.Option("--repetitions", min=5)] = 5,
    apply: Annotated[
        bool, typer.Option("--uygula", help="Yetkili local benchmark execution calistirir")
    ] = False,
    authorization_id: Annotated[
        UUID | None, typer.Option("--authorization-id", help="Exact one-shot authorization UUID")
    ] = None,
    project_uuid: Annotated[
        UUID | None, typer.Option("--project-uuid", help="Runtime job project UUID")
    ] = None,
    adapter_executable: Annotated[
        Path | None, typer.Option("--adapter-executable", help="Tested model JSON adapter")
    ] = None,
    adapter_script: Annotated[
        Path | None, typer.Option("--adapter-script", help="Tested adapter script path")
    ] = None,
    verifier_executable: Annotated[
        Path | None, typer.Option("--verifier-executable", help="Verifier JSON adapter")
    ] = None,
    verifier_script: Annotated[
        Path | None, typer.Option("--verifier-script", help="Verifier adapter script path")
    ] = None,
    verifier_model: Annotated[
        str | None, typer.Option("--verifier-model", help="Independent verifier model ID")
    ] = None,
    verifier_identity: Annotated[
        str | None, typer.Option("--verifier-identity", help="Verifier execution identity")
    ] = None,
    verifier_provenance: Annotated[
        str | None, typer.Option("--verifier-provenance", help="Verifier provenance SHA-256")
    ] = None,
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Versioned fixture registry'yi veya digest-bound benchmark planini hazirlar."""
    try:
        registry = load_fixture_registry()
        if model is None:
            document: dict[str, object] = {
                "schema": "zekam-model-benchmark-registry/v1",
                "registry_digest": registry.registry_digest,
                "fixture_count": len(registry.fixtures),
                "local_fixture_count": len(registry.eligible(remote=False)),
                "remote_fixture_count": len(registry.eligible(remote=True)),
                "dry_run": True,
            }
        else:
            if project is not None:
                if capability_digest is None:
                    raise fail("Project suite --capability-digest ister", 64)
                suite = build_project_suite(
                    project_id=project,
                    capability_profile_digest=capability_digest,
                    registry=registry,
                )
            else:
                suite = BenchmarkSuite(
                    suite_id="general",
                    version=registry.schema_version,
                    kind=SuiteKind.GENERAL,
                    fixture_digests=tuple(
                        item.fixture_digest
                        for item in registry.fixtures
                        if "opencode-remote" not in item.tags
                    ),
                )
            if inventory_digest is None or policy_digest is None:
                raise fail("Plan --inventory-digest ve --policy-digest ister", 64)
            plan = BenchmarkPlan(
                model_id=model,
                suite_digest=suite.suite_digest,
                inventory_digest=inventory_digest,
                policy_digest=policy_digest,
                fixture_registry_digest=registry.registry_digest,
                repetitions=repetitions,
            )
            document = {
                "schema": "zekam-model-benchmark-plan/v1",
                "suite": suite.as_dict(),
                "suite_digest": suite.suite_digest,
                "plan_digest": plan.plan_digest,
                "repetitions": plan.repetitions,
                "dry_run": True,
            }
            if apply:
                if (
                    authorization_id is None
                    or project_uuid is None
                    or adapter_executable is None
                    or verifier_executable is None
                    or verifier_model is None
                    or verifier_identity is None
                    or verifier_provenance is None
                ):
                    raise PolicyViolation(
                        "Benchmark --uygula exact authorization, tested adapter ve independent"
                        " verifier ister"
                    )
                with RealmSession(home, realm) as realm_context:
                    _service(realm_context).require_benchmark_eligible(
                        model,
                        inventory_digest=inventory_digest,
                    )
                    repository = BenchmarkRepository(
                        realm_context.connection, realm_context.realm_id
                    )
                    resource = (
                        f"model-benchmark:{plan.model_id}:"
                        f"{plan.suite_digest.removeprefix('sha256:')}"
                    )
                    authorizations = AuthorizationRepository(
                        realm_context.connection, realm_context.realm_id
                    )
                    authorization = authorizations.get(authorization_id)
                    if authorization.plan_digest != plan.plan_digest:
                        raise PolicyViolation("Authorization plan digest eslesmiyor")
                    effect_digest = digest(
                        [{"effect": EffectKind.DATABASE_WRITE.value, "resources": [resource]}]
                    )
                    consumed = authorizations.consume(
                        authorization.id,
                        effect_digest=effect_digest,
                        consumed_by="cli:model-benchmark-local",
                    )
                    if not consumed.consumed or consumed.authorization is None:
                        raise PolicyViolation(f"Authorization tuketilemedi: {consumed.reason}")
                    capability = f"model.benchmark.local.{plan.plan_digest[-16:]}"
                    host = ExecutionHost(
                        realm_context.connection,
                        realm_context.realm_id,
                        worker_label="model-benchmark-local",
                    )
                    job, _ = host.jobs.enqueue(
                        Job.create(
                            realm_id=realm_context.realm_id,
                            project_id=project_uuid,
                            kind=JobKind.MUTATION,
                            idempotency_key=f"model-benchmark:{plan.plan_digest}",
                            resources=parse_requests(write=(resource,)),
                            required_capabilities=(capability,),
                        )
                    )
                    work = host.acquire_work(capabilities=(capability,))
                    if work is None or work.job.id != job.id:
                        raise PolicyViolation("Benchmark runtime job claim edilemedi")
                    oracle = DeterministicLocalBenchmarkAdapter(
                        default_fixture_file().parent.resolve(strict=True)
                    )
                    adapter = LocalProcessBenchmarkAdapter(
                        routed_model_id=model,
                        argv=(
                            str(adapter_executable.resolve(strict=True)),
                            *(
                                ()
                                if adapter_script is None
                                else (str(adapter_script.resolve(strict=True)),)
                            ),
                        ),
                        oracle=oracle,
                    )
                    verifier_adapter = LocalProcessBenchmarkVerifier(
                        identity=VerifierIdentity(
                            verifier_model, verifier_identity, verifier_provenance
                        ),
                        argv=(
                            str(verifier_executable.resolve(strict=True)),
                            *(
                                ()
                                if verifier_script is None
                                else (str(verifier_script.resolve(strict=True)),)
                            ),
                        ),
                    )
                    service = BenchmarkExecutionService(repository, registry)
                    gateway = RuntimeBenchmarkClaimGateway(
                        host=host,
                        work=work,
                        authorization=consumed.authorization,
                        adapter_digest=adapter.adapter_digest,
                    )
                    plan_id, trials = service.execute(
                        suite=suite,
                        plan=plan,
                        adapter=adapter,
                        verifier_adapter=verifier_adapter,
                        claims=gateway,
                    )
                    result_digest = digest(
                        [
                            item.evidence_digest
                            for item in sorted(
                                trials,
                                key=lambda row: (row.fixture_digest, row.repetition),
                            )
                        ]
                    )
                    host.finish(work, outcome=AttemptOutcome.SUCCEEDED, result_digest=result_digest)
                document |= {
                    "dry_run": False,
                    "plan_id": str(plan_id),
                    "trial_count": len(trials),
                    "result_digest": result_digest,
                }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    else:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("decide")
def decide_command(
    input_file: Annotated[
        Path,
        typer.Option(
            "--girdi", exists=True, dir_okay=False, help="Yalniz karar gereksinimleri JSON'u"
        ),
    ],
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Hard gate'leri kanonik ledger'dan kuran authority-free Model Decision."""
    try:
        source = json.loads(input_file.read_text(encoding="utf-8"))
        if "candidates" in source or "quota_observations" in source:
            raise PolicyViolation("CLI aday, hard gate veya quota evidence kabul etmez")
        requirements = DecisionRequirements(
            workload=str(source["workload"]),
            client=str(source["client"]),
            modality=str(source["modality"]),
            project_id=str(source["project_id"]),
            required_capabilities=tuple(str(item) for item in source["required_capabilities"]),
            verifier_model_id=str(source["verifier_model_id"]),
            local_data_required=bool(source["local_data_required"]),
            max_latency_ms=float(source["max_latency_ms"]),
            max_cost=float(source["max_cost"]),
            max_tokens=float(source["max_tokens"]),
            evidence_digest=str(source["evidence_digest"]),
        )
        with RealmSession(home, realm) as realm_context:
            repository = BenchmarkRepository(realm_context.connection, realm_context.realm_id)
            _, decision = ModelDecisionService(repository).decide(requirements)
        document = {
            "schema": "zekam-model-decision/v1",
            "selected_model_id": decision.selected_model_id,
            "selected_score": decision.selected_score,
            "rejected": decision.rejected,
            "evidence_digest": decision.evidence_digest,
            "authority_granted": decision.authority_granted,
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise fail(f"Model decision girdisi gecersiz: {type(exc).__name__}", 64) from exc
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    else:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))


def _service(realm_context: RealmContext) -> ModelHealthService:
    """Production health okumalari icin fail-closed servis.

    Probe calistirmak icin exact authorized provider adapteri ayrica enjekte edilmelidir.
    """
    return ModelHealthService(
        realm_context.connection,
        realm_context.realm,
        probe=AuthorizationRequiredProviderProbe(),
    )


@app.command("provider-config")
def provider_config_command(
    source: Annotated[
        Path | None, typer.Option("--kaynak", help="Provider binding YAML yolu")
    ] = None,
    require_ready: Annotated[
        bool,
        typer.Option(
            "--hazir-olmasini-iste",
            help="Eksik endpoint/credential/SecretRef varsa hata kodu dondurur",
        ),
    ] = False,
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Model endpoint eslemesini ag ve provider cagrisi yapmadan dogrular."""
    try:
        bindings = load_provider_bindings(source)
        inventory = load_inventory()
        with RealmSession(home, realm) as realm_context:
            references = {
                item.name: item
                for item in SecretRefRepository(
                    realm_context.connection, realm_context.realm_id
                ).list_all()
            }
        report = evaluate_provider_configuration(
            bindings=bindings,
            inventory=inventory,
            secret_refs=references,
        )
        document = report.as_dict()
    except ZekamError as exc:
        raise fail_from(exc) from exc

    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    else:
        table = Table(title="Provider yapilandirma dry-run")
        table.add_column("Modalite")
        table.add_column("Model")
        table.add_column("Endpoint locator")
        table.add_column("Credential locator")
        table.add_column("Durum")
        table.add_column("Eksikler")
        for check in report.checks:
            table.add_row(
                check.binding.modality.value,
                check.binding.access_name,
                check.binding.endpoint_env,
                check.binding.credential_env,
                "hazir" if check.ready else "eksik",
                ", ".join(check.reasons) or "-",
            )
        console.print(table)
        console.print(
            f"Dry-run: {report.ready_count}/{len(report.checks)} hazir; "
            "provider_calls=0, network_calls=0"
        )
    if require_ready and not report.ready:
        raise fail("Provider yapilandirmasi henuz hazir degil", 6)


@app.command("provider-plan")
def provider_plan_command(
    source: Annotated[
        Path | None, typer.Option("--kaynak", help="Provider binding YAML yolu")
    ] = None,
    fixture_source: Annotated[
        Path | None, typer.Option("--fixture-kaynagi", help="Public contract fixture YAML yolu")
    ] = None,
    require_ready: Annotated[
        bool,
        typer.Option(
            "--hazir-olmasini-iste",
            help="Canli son kapi icin tum yerel onkosullari zorunlu tutar",
        ),
    ] = False,
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Exact policy/call planini uretir; authorization veya canli cagri uretmez."""
    try:
        bindings = load_provider_bindings(source)
        fixtures = load_provider_contract_fixtures(fixture_source)
        inventory = load_inventory()
        with RealmSession(home, realm) as realm_context:
            references = {
                item.name: item
                for item in SecretRefRepository(
                    realm_context.connection, realm_context.realm_id
                ).list_all()
            }
            governance = GovernanceService(realm_context.connection, realm_context.realm)
            base_policy = governance.policies.current(DEFAULT_POLICY_NAME)
            base_policy_persisted = base_policy is not None
            if base_policy is None:
                base_policy = PolicyDocument.create(
                    realm_id=realm_context.realm_id,
                    name=DEFAULT_POLICY_NAME,
                    revision=1,
                    rules=default_policy_rules(),
                )
        report = evaluate_provider_configuration(
            bindings=bindings,
            inventory=inventory,
            secret_refs=references,
        )
        manifest = build_provider_execution_manifest(
            bindings, fixtures, inventory=inventory, environ=os.environ
        )
        policy_candidate = build_provider_policy_candidate(base_policy, manifest)
        audio_plan = next(
            item for item in manifest.calls if item.modality is Modality.AUDIO_TRANSCRIPTION
        )
        audio_ready = audio_plan.runtime_bound
        prelive_ready = report.ready and all(item.runtime_bound for item in manifest.calls)
        document = {
            "schema": "zekam-provider-prelive-plan/v1",
            "configuration": report.as_dict(),
            "fixture_digest": fixtures.fixture_digest,
            "manifest_digest": manifest.manifest_digest,
            "call_count": len(manifest.calls),
            "target_count": len(manifest.targets),
            "calls": [item.as_dict() for item in manifest.calls],
            "policy": {
                "base_policy_digest": base_policy.policy_digest,
                "base_policy_persisted": base_policy_persisted,
                "candidate_policy_digest": policy_candidate.policy_digest,
                "candidate_revision": policy_candidate.revision,
                "exact_provider_targets": list(manifest.targets),
                "network_default_deny": policy_candidate.network_default_deny,
                "push_default_deny": policy_candidate.push_default_deny,
                "persisted": False,
            },
            "audio_fixture_present": audio_ready,
            "prelive_ready": prelive_ready,
            "authorization_records_created": 0,
            "provider_calls_made": 0,
            "network_calls_made": 0,
            "live_test_deferred": True,
            "grants_authority": False,
        }
    except ZekamError as exc:
        raise fail_from(exc) from exc

    console.print_json(json.dumps(document, ensure_ascii=False, default=str))
    if require_ready and not prelive_ready:
        raise fail("Canli son kapi onkosullari henuz hazir degil", 6)


@app.command("provider-authorize")
def provider_authorize_command(
    work_id: Annotated[UUID, typer.Option("--work", help="Kanonik Work Item UUID")],
    actor_id: Annotated[UUID, typer.Option("--actor", help="Yetkili kanonik actor UUID")],
    apply: Annotated[
        bool,
        typer.Option("--uygula", help="Policy, TaskPlan ve 10 exact authorization kaydeder"),
    ] = False,
    output_json: Annotated[bool, typer.Option("--json", help="Sanitize JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Readiness gectikten sonra Work-bound exact live plan ve auth seti uretir."""

    del output_json
    if not apply:
        raise fail("Provider authorize exact --work, --actor ve --uygula ister", 64)
    try:
        bindings = load_provider_bindings()
        fixtures = load_provider_contract_fixtures()
        inventory = load_inventory()
        manifest = build_provider_execution_manifest(
            bindings, fixtures, inventory=inventory, environ=os.environ
        )
        if len(manifest.calls) != 10 or not all(item.runtime_bound for item in manifest.calls):
            raise PolicyViolation("Provider authorize exact runtime-bound 10 call ister")
        source_head, source_revision = _provider_source_revision()
        with RealmSession(home, realm) as realm_context:
            references = {
                item.name: item
                for item in SecretRefRepository(
                    realm_context.connection, realm_context.realm_id
                ).list_all()
            }
            readiness = evaluate_provider_configuration(
                bindings=bindings,
                inventory=inventory,
                secret_refs=references,
                environ=os.environ,
            )
            if not readiness.ready:
                raise PolicyViolation("Provider readiness gecmeden mutation yasak")
            governance = GovernanceService(
                realm_context.connection, realm_context.realm, actor_id=actor_id
            )
            base_policy = governance.policies.current(DEFAULT_POLICY_NAME)
            if base_policy is None:
                raise PolicyViolation("Kanonik varsayilan policy bulunamadi")
            candidate_policy = build_provider_policy_candidate(base_policy, manifest)
            graph = WorkGraphService(
                realm_context.connection, realm_context.realm, actor_id=actor_id
            )
            graph.items.get(work_id)
            steps = _provider_plan_steps(manifest)
            authorizations: list[tuple[str, Authorization]] = []
            with realm_context.connection.transaction():
                governance.policies.append(candidate_policy)
                task_plan = graph.create_plan(
                    work_id,
                    source_revision=source_revision,
                    policy_digest=candidate_policy.policy_digest,
                    steps=steps,
                )
                repository = AuthorizationRepository(
                    realm_context.connection, realm_context.realm_id
                )
                for call in manifest.calls:
                    secret_ref = references.get(call.secret_ref_name)
                    if secret_ref is None:
                        raise PolicyViolation("Provider exact SecretRef metadata eksik")
                    exact = Authorization.issue(
                        realm_id=realm_context.realm_id,
                        actor_id=actor_id,
                        work_item_id=work_id,
                        plan_id=task_plan.id,
                        plan_digest=call.authorization_plan_digest,
                        effect_digest=call.effect_request.effect_digest,
                        scope=AuthorizationScope(
                            allowed_resources=(call.target, call.call_resource),
                            allowed_effects=(EffectKind.PROVIDER_CALL.value,),
                            provider_refs=(call.provider_ref,),
                            secret_ref_ids=(secret_ref.id,),
                            data_classifications=(DataClassification.PUBLIC,),
                        ),
                        risk="critical",
                        lifetime=dt.timedelta(minutes=30),
                    )
                    repository.issue(exact)
                    authorizations.append((call.call_id, exact))
        document = {
            "schema": "zekam-provider-live-authorization-set/v1",
            "work_id": str(work_id),
            "task_plan_id": str(task_plan.id),
            "task_plan_digest": task_plan.plan_digest,
            "source_head": source_head,
            "source_revision": source_revision,
            "policy_digest": candidate_policy.policy_digest,
            "manifest_digest": manifest.manifest_digest,
            "call_count": 10,
            "authorizations": [
                {
                    "call_id": call_id,
                    "authorization_id": str(item.id),
                    "plan_digest": item.plan_digest,
                    "effect_digest": item.effect_digest,
                    "max_uses": 1,
                }
                for call_id, item in authorizations
            ],
            "grants_authority": False,
            "provider_calls_made": 0,
            "network_calls_made": 0,
        }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("provider-live-run")
def provider_live_run_command(
    authorization: Annotated[
        list[str],
        typer.Option(
            "--authorization",
            help="Exact CALL_ID=AUTHORIZATION_UUID; tam 10 kez verilir",
        ),
    ],
    project_uuid: Annotated[UUID, typer.Option("--project-uuid", help="Runtime job project UUID")],
    work_id: Annotated[UUID, typer.Option("--work", help="Kanonik Work Item UUID")],
    plan_id: Annotated[UUID, typer.Option("--plan-id", help="Exact TaskPlan UUID")],
    source_revision: Annotated[
        str, typer.Option("--source-revision", help="Authorize ciktisindaki source revision")
    ],
    policy_digest: Annotated[
        str, typer.Option("--policy-digest", help="Authorize ciktisindaki policy digest")
    ],
    apply: Annotated[
        bool, typer.Option("--uygula", help="Exact 10 canli contract cagrisini yurutur")
    ] = False,
    output_json: Annotated[bool, typer.Option("--json", help="Sanitize JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Yalniz reviewed manifestteki 10 exact call'i claim/receipt ile yurutur."""

    if not apply:
        raise fail(
            "Canli provider run yalniz --uygula ile ve exact authorization setiyle acilir",
            64,
        )
    try:
        auth_by_call: dict[str, UUID] = {}
        for auth_spec in authorization:
            call_id, separator, raw_id = auth_spec.partition("=")
            if not separator or call_id in auth_by_call:
                raise PolicyViolation("Authorization listesi exact CALL_ID=UUID olmali")
            auth_by_call[call_id] = UUID(raw_id)
        bindings = load_provider_bindings()
        fixtures = load_provider_contract_fixtures()
        inventory = load_inventory()
        manifest = build_provider_execution_manifest(
            bindings, fixtures, inventory=inventory, environ=os.environ
        )
        _, current_source_revision = _provider_source_revision()
        if current_source_revision != source_revision:
            raise PolicyViolation("Provider live source revision drift")
        expected_ids = {item.call_id for item in manifest.calls}
        if set(auth_by_call) != expected_ids or len(auth_by_call) != 10:
            raise PolicyViolation("Canli provider run exact 10 call authorization ister")
        prepared = prepare_provider_contract_calls(
            manifest=manifest,
            bindings=bindings,
            fixtures=fixtures,
            inventory=inventory,
            environ=os.environ,
        )
        with RealmSession(home, realm) as realm_context:
            secret_repository = SecretRefRepository(
                realm_context.connection, realm_context.realm_id
            )
            references = {item.name: item for item in secret_repository.list_all()}
            if {item.plan.secret_ref_name for item in prepared} - set(references):
                raise PolicyViolation("Canli provider run exact SecretRef metadata seti ister")
            readiness = evaluate_provider_configuration(
                bindings=bindings,
                inventory=inventory,
                secret_refs=references,
                environ=os.environ,
            )
            if not readiness.ready or not all(item.plan.runtime_bound for item in prepared):
                raise PolicyViolation("Provider live readiness drift; mutation yasak")
            authorization_repository = AuthorizationRepository(
                realm_context.connection, realm_context.realm_id
            )
            governance = GovernanceService(realm_context.connection, realm_context.realm)
            current_policy = governance.policies.current(DEFAULT_POLICY_NAME)
            if current_policy is None or current_policy.policy_digest != policy_digest:
                raise PolicyViolation("Provider live policy digest drift")
            graph = WorkGraphService(realm_context.connection, realm_context.realm)
            task_plan = graph.assert_plan_is_current(
                work_id,
                source_revision=current_source_revision,
                policy_digest=policy_digest,
            )
            if (
                task_plan.id != plan_id
                or task_plan.project_id != project_uuid
                or task_plan.steps != _provider_plan_steps(manifest)
            ):
                raise PolicyViolation("Provider live TaskPlan/work/manifest binding mismatch")
            exact_authorizations: dict[str, Authorization] = {}
            for prepared_call in prepared:
                exact_authorization = authorization_repository.get(
                    auth_by_call[prepared_call.plan.call_id]
                )
                if (
                    exact_authorization.work_item_id != work_id
                    or exact_authorization.plan_id != plan_id
                ):
                    raise PolicyViolation("Provider live authorization Work/TaskPlan mismatch")
                verify_exact_provider_authorization(
                    prepared_call,
                    exact_authorization,
                    references[prepared_call.plan.secret_ref_name],
                )
                exact_authorizations[prepared_call.plan.call_id] = exact_authorization
            client = AuthorizedProviderClient(
                governance,
                EnvironmentEndpointResolver(
                    {
                        (item.endpoint_ref, item.operation): item.endpoint_env
                        for item in bindings.bindings
                    }
                ),
                SecretBroker({SecretBackend.ENVIRONMENT: EnvironmentSecretStore()}),
                UrllibJsonProviderTransport(),
                UrllibMultipartProviderTransport(),
            )
            capability = f"provider.contract.live.{manifest.manifest_digest[-16:]}"
            host = ExecutionHost(
                realm_context.connection,
                realm_context.realm_id,
                worker_label="provider-contract-live",
            )
            job, _ = host.jobs.enqueue(
                Job.create(
                    realm_id=realm_context.realm_id,
                    project_id=project_uuid,
                    kind=JobKind.PROVIDER_CALL,
                    idempotency_key=f"provider-contract-live:{manifest.manifest_digest}",
                    resources=parse_requests(
                        write=tuple(item.call_resource for item in manifest.calls)
                    ),
                    required_capabilities=(capability,),
                    max_attempts=1,
                    work_item_id=work_id,
                    plan_id=plan_id,
                    step_id="provider-live-contracts",
                )
            )
            work = host.acquire_work(capabilities=(capability,))
            if work is None or work.job.id != job.id:
                raise PolicyViolation("Provider contract runtime job claim edilemedi")
            runner = RuntimeProviderContractRunner(host=host, work=work, client=client)
            executions = []
            responses = {}
            for prepared_call in prepared:
                exact_authorization = exact_authorizations[prepared_call.plan.call_id]
                execution = runner.invoke(
                    prepared_call,
                    secret_ref=references[prepared_call.plan.secret_ref_name],
                    authorization=exact_authorization,
                    consumed_by=f"cli:provider-live:{prepared_call.plan.call_id}",
                )
                executions.append(execution)
                responses[prepared_call.plan.call_id] = execution.provider_result.response
            observations = assemble_contract_observations(prepared, responses, fixtures)
            evaluations = [
                (modality, evaluate_observation(observation))
                for modality, observation in sorted(
                    observations.items(), key=lambda pair: pair[0].value
                )
            ]
            text_contracts = evaluate_text_contracts(prepared, responses, fixtures)
            all_verified = all(item.verified for _, item in evaluations) and all(
                text_contracts.values()
            )
            run_digest = digest(
                {
                    "manifest_digest": manifest.manifest_digest,
                    "receipts": [str(item.receipt.id) for item in executions],
                    "evaluation_digests": [item.evidence_digest for _, item in evaluations],
                    "text_contracts": text_contracts,
                }
            )
            if all_verified:
                plan_step_ids = tuple(step.step_id for step in task_plan.steps)
                call_result_digests: dict[str, str] = {}
                for item in executions:
                    evidence_digest = item.receipt.adapter_evidence_digest
                    if evidence_digest is None:
                        raise PolicyViolation("Provider completed receipt evidence digest ister")
                    call_result_digests[item.call_id] = evidence_digest
                step_results = tuple(
                    (
                        step_id,
                        run_digest
                        if step_id == "provider-live-contracts"
                        else call_result_digests[step_id],
                    )
                    for step_id in plan_step_ids
                )
                checkpoint = Checkpoint(
                    checkpoint_id=f"provider-live-{job.id}",
                    project_id=str(project_uuid),
                    work_item_id=str(work_id),
                    plan_revision_id=str(plan_id),
                    source_revision=current_source_revision,
                    plan_steps=plan_step_ids,
                    completed_steps=plan_step_ids,
                    pending_steps=(),
                    step_results=step_results,
                    context_manifest_digest=manifest.manifest_digest,
                    journal_head_digest=run_digest,
                    next_safe_action="independent-verification",
                    created_at=dt.datetime.now(dt.UTC),
                )
                ContextContinuityRepository(
                    realm_context.connection,
                    realm_context.realm_id,
                    project_uuid,
                    work_id,
                ).store_checkpoint(checkpoint, task_plan_id=plan_id, job_id=job.id)
            host.finish(
                work,
                outcome=(AttemptOutcome.SUCCEEDED if all_verified else AttemptOutcome.FAILED),
                result_digest=run_digest if all_verified else None,
                failure_category=None if all_verified else FailureCategory.VALIDATION,
            )
            document = {
                "schema": "zekam-provider-live-acceptance/v1",
                "status": "passed" if all_verified else "failed",
                "manifest_digest": manifest.manifest_digest,
                "provider_calls_made": len(executions),
                "network_calls_made": len(executions),
                "calls": [
                    {
                        "call_id": item.call_id,
                        "authorization_id": str(item.provider_result.authorization_id),
                        "claim_id": str(item.claim.id),
                        "receipt_id": str(item.receipt.id),
                        "receipt_status": item.receipt.status.value,
                        "response_digest": item.provider_result.response_digest,
                        "provider_evidence_digest": item.receipt.adapter_evidence_digest,
                        "plan_digest": next(
                            row.plan.authorization_plan_digest
                            for row in prepared
                            if row.plan.call_id == item.call_id
                        ),
                    }
                    for item in executions
                ],
                "evaluations": [
                    {
                        "modality": modality.value,
                        "verified": evaluation.verified,
                        "metrics": evaluation.metrics,
                        "fixture_digest": evaluation.fixture_digest,
                        "response_digest": evaluation.response_digest,
                        "evidence_digest": evaluation.evidence_digest,
                    }
                    for modality, evaluation in evaluations
                ],
                "text_contracts": text_contracts,
                "verifier": {"verified": False, "evidence_digest": None},
                "raw_prompts_reported": 0,
                "raw_responses_reported": 0,
                "secret_values_reported": 0,
                "endpoint_values_reported": 0,
                "run_digest": run_digest,
            }
    except (ValueError, ZekamError) as exc:
        if isinstance(exc, ZekamError):
            raise fail_from(exc) from exc
        raise fail("Authorization UUID gecersiz", 64) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("inventory")
def inventory_command(
    apply: Annotated[
        bool, typer.Option("--uygula", help="Envanteri kanonik store'a aktarir")
    ] = False,
    source: Annotated[Path | None, typer.Option("--kaynak", help="Envanter dosyasi yolu")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kanonik envanteri dogrular ve istege bagli olarak iceri aktarir."""
    try:
        snapshot = load_inventory(source)
        discrepancies = verify_snapshot(snapshot)
    except ZekamError as exc:
        raise fail_from(exc) from exc

    summary = summarize_snapshot(snapshot)
    summary["discrepancies"] = [item.as_dict() for item in discrepancies]

    if not apply:
        if output_json:
            # JSON ciktisi ayristirilabilir kalmali; uyari stdout'a karismaz.
            summary["dry_run"] = True
            console.print_json(json.dumps(summary, ensure_ascii=False, default=str))
            return
        _render_summary(summary)
        for item in discrepancies:
            console.print(f"[yellow]uyari:[/yellow] {item.kind} — {item.detail}")
        console.print("[yellow]Dry-run. Aktarmak icin --uygula verin.[/yellow]")
        return

    try:
        with RealmSession(home, realm, create_realm=True) as realm_context:
            repository = ModelInventoryRepository(realm_context.connection, realm_context.realm_id)
            counts = {"inserted": 0, "updated": 0, "unchanged": 0}
            for record in snapshot.records:
                counts[repository.upsert(record)] += 1
            report = ImportReport(
                inserted=counts["inserted"],
                updated=counts["updated"],
                unchanged=counts["unchanged"],
                discrepancies=discrepancies,
                snapshot_digest=snapshot.snapshot_digest,
            )
    except ZekamError as exc:
        raise fail_from(exc) from exc

    if output_json:
        console.print_json(json.dumps(report.as_dict(), ensure_ascii=False, default=str))
        return
    console.print(
        f"[green]Aktarildi:[/green] {report.inserted} yeni, {report.updated} guncel, "
        f"{report.unchanged} degismedi"
    )
    for item in report.discrepancies:
        console.print(f"[yellow]uyari:[/yellow] {item.kind} — {item.detail}")


def _render_summary(summary: dict[str, object]) -> None:
    table = Table(title="Model envanteri")
    table.add_column("Alan")
    table.add_column("Deger")
    table.add_row("kanonik kayit", str(summary["canonical_count"]))
    table.add_row("teknik profil", str(summary["technical_profile_count"]))
    missing = summary["missing_technical_profile"]
    assert isinstance(missing, list)
    table.add_row("profil farki", str(len(missing)))
    duplicated = summary["duplicated_backends"]
    assert isinstance(duplicated, dict)
    table.add_row("ayni backend", str(len(duplicated)))
    modalities = summary["modalities"]
    assert isinstance(modalities, dict)
    for name, count in modalities.items():
        table.add_row(f"modalite: {name}", str(count))
    console.print(table)


@app.command("list")
def list_command(
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kayitli modelleri listeler. Ham endpoint gosterilmez."""
    try:
        with RealmSession(home, realm) as realm_context:
            records = ModelInventoryRepository(
                realm_context.connection, realm_context.realm_id
            ).list_all()
            rows = [record.as_dict() for record in records]
    except ZekamError as exc:
        raise fail_from(exc) from exc

    if output_json:
        console.print_json(json.dumps(rows, ensure_ascii=False, default=str))
        return
    table = Table(title="Modeller")
    table.add_column("#")
    table.add_column("Erisim adi")
    table.add_column("Modalite")
    table.add_column("Saglik")
    table.add_column("Teknik profil")
    for record in records:
        table.add_row(
            str(record.inventory_index),
            record.access_name,
            record.modality.value,
            record.health_state.value,
            "var" if record.has_technical_profile else "yok",
        )
    console.print(table)


@app.command("health")
def health_command(
    model: Annotated[
        str | None, typer.Option("--model", help="Tek bir Model ID; verilmezse hepsi")
    ] = None,
    apply: Annotated[bool, typer.Option("--uygula", help="Probe'u gercekten calistirir")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Health durumunu gosterir; live probe exact provider gate olmadan calismaz."""
    if not apply:
        console.print(
            "Dry-run; calistirilacak: exact authorization/claim/receipt provider health probe"
        )
        console.print(
            "[yellow]Canli health icin provider-authorize ve provider-live-run zincirini kullanin."
            "[/yellow]"
        )
        return
    del model, realm, home
    raise fail(
        "Production health sentetik probe ile yazilamaz; exact live provider authorization,"
        " claim ve terminal receipt gerekir",
        6,
    )


@app.command("report")
def report_command(
    apply: Annotated[bool, typer.Option("--uygula", help="Raporu kaydeder")] = False,
    output: Annotated[Path | None, typer.Option("--cikti", help="Markdown dosyasi yolu")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="JSON yazar")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Gunluk model saglik raporunu uretir. Markdown ve JSON ayni kanita baglanir."""
    try:
        with RealmSession(home, realm) as realm_context:
            service = _service(realm_context)
            records = service.inventory.list_all()
            if not records:
                raise fail("Kayitli model yok; once `model inventory --uygula` calistirin", 4)
            report = build_report(
                records,
                report_date=dt.date.today(),
                stale_model_ids=service.stale_models(),
            )
            if apply:
                HealthReportRepository(realm_context.connection, realm_context.realm_id).store(
                    report_date=report.report_date,
                    summary=report.summary(),
                    evidence_digest=report.evidence_digest,
                    markdown_digest=report.markdown_digest,
                    json_digest=report.json_digest,
                )
    except ZekamError as exc:
        raise fail_from(exc) from exc

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report.as_markdown(), encoding="utf-8", newline="\n")
        console.print(f"[green]Rapor yazildi:[/green] {output}")
    if output_json:
        console.print_json(report.as_json())
    elif output is None:
        console.print(report.as_markdown())
