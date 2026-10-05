"""Provider-free ``zekam test`` CLI ve ledger okuma yuzeyi (W07)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer

from zekam.application.code_graph import graph_store_path
from zekam.application.composition import build_context
from zekam.application.unit_test_intent import (
    UnitTestIntentKind,
    classify_unit_test_request,
)
from zekam.application.unit_test_loop_state import LedgerLoopControl
from zekam.application.unit_test_outcome import (
    EXIT_CLARIFICATION,
    EXIT_ENVIRONMENT_MISSING,
    EXIT_NOT_FOUND,
    EXIT_POLICY,
    EXIT_RUNTIME,
    EXIT_USAGE,
    exit_code_for_reason,
    gate_document,
    terminal_document,
)
from zekam.application.unit_test_plateau import LoopLimits, UnitTestApprovals
from zekam.application.unit_test_runtime import (
    NATIVE_EVIDENCE_CONTRACT,
    NATIVE_MODE_MARKER,
    MavenMeasurementBinding,
    UnitTestRuntimeBinding,
    compose_native_measurement,
    compose_unit_test_runtime,
    measure_native,
    native_request,
)
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import ZekamError
from zekam.domain.unit_test_engineering import (
    CoverageMetric,
    CoveragePolicy,
    UnitTestBudget,
    UnitTestRequest,
    UnitTestStopReason,
    UnitTestTerminal,
)
from zekam.infrastructure.sqlite.code_graph import SQLiteCodeGraphStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.infrastructure.unit_test_runner.maven_plan import build_unit_test_plan

app = typer.Typer(
    name="test",
    help="Unit-test plan/ledger yuzeyi; plan varsayilan olarak provider-free'dir",
    no_args_is_help=True,
)


def _emit(document: object) -> None:
    typer.echo(json.dumps(document, ensure_ascii=True, sort_keys=True, default=str))


def _finish(document: dict[str, object], code: int) -> None:
    _emit(document)
    if code:
        raise typer.Exit(code)


def _clarification(reasons: tuple[str, ...], *, next_steps: tuple[str, ...]) -> None:
    _finish(
        gate_document("clarification-required", reasons=reasons, next_steps=next_steps),
        EXIT_CLARIFICATION,
    )


def _plan_request(
    *,
    request_text: str | None,
    project_id: str | None,
    source_binding_id: str | None,
    source_revision: str | None,
    source_files: tuple[str, ...],
    percent: str | None,
    metric: str | None,
    policy: str | None,
    max_attempts: int,
    process_timeout_seconds: int,
    total_elapsed_seconds: int,
    allowed_test_paths: tuple[str, ...],
    forbidden_paths: tuple[str, ...],
    work_item_id: str | None = None,
    plan_id: str | None = None,
    run_id: str | None = None,
    source_snapshot_id: str | None = None,
    graph_generation_digest: str | None = None,
    native: bool = False,
) -> tuple[UnitTestRequest, dict[str, object]]:
    intent_document: dict[str, object] = {}
    inferred_files = source_files
    inferred_percent = percent
    inferred_metric = metric
    inferred_policy = policy
    if request_text is not None:
        intent = classify_unit_test_request(request_text)
        intent_document = intent.as_dict()
        if intent.kind is UnitTestIntentKind.CONCEPT_QUESTION:
            _clarification(
                ("unit-test-concept-question",),
                next_steps=("Yazma istegi ve exact hedef dosyalarini belirtin.",),
            )
        if intent.kind is not UnitTestIntentKind.WRITE_REQUEST:
            _clarification(
                ("not-a-write-request",),
                next_steps=("Unit-test yazma/kapsam istegini ayri ve acik belirtin.",),
            )
        if intent.clarifications:
            _clarification(
                intent.clarifications,
                next_steps=("Exact Java kaynak dosyasi/sinifi ve coverage esigini belirtin.",),
            )
        inferred_files = source_files or intent.files
        inferred_percent = percent or intent.percent
        inferred_metric = metric or intent.metric
        inferred_policy = policy or intent.policy

    missing = tuple(
        label
        for label, value in (
            ("project-id-missing", project_id),
            ("source-binding-id-missing", source_binding_id),
            ("source-revision-missing", source_revision),
            ("target-files-missing", inferred_files),
            ("threshold-missing", inferred_percent),
        )
        if not value
    )
    if missing:
        _clarification(
            missing,
            next_steps=(
                "--project-id, --source-binding-id, --source-revision, --source ve "
                "--percent verin.",
            ),
        )

    try:
        resolved_metric = CoverageMetric(inferred_metric or CoverageMetric.LINE)
        resolved_policy = CoveragePolicy(inferred_policy or CoveragePolicy.PER_FILE)
        budget = UnitTestBudget(
            max_attempts,
            process_timeout_seconds,
            total_elapsed_seconds,
        )
        request = UnitTestRequest.with_defaults(
            project_id=project_id or "",
            source_binding_id=source_binding_id or "",
            source_revision=source_revision or "",
            source_files=inferred_files,
            percent=inferred_percent or "",
            budget=budget,
            metric=resolved_metric,
            policy=resolved_policy,
            allowed_test_paths=allowed_test_paths,
            forbidden_paths=forbidden_paths,
            work_item_id=work_item_id,
            plan_id=plan_id,
            run_id=run_id,
            source_snapshot_id=source_snapshot_id,
            graph_generation_digest=graph_generation_digest,
        )
    except (TypeError, ValueError, ZekamError) as exc:
        _finish(
            gate_document(
                "usage-error",
                reasons=(str(exc),),
                next_steps=("Plan parametrelerini exact ve portable degerlerle tekrarlayin.",),
            ),
            EXIT_USAGE,
        )
        raise AssertionError("unreachable") from exc
    if native:
        request = native_request(request)
    return request, intent_document


def _execution_plan(project_root: str | None, target_modules: tuple[str, ...]) -> dict[str, object]:
    """Maven/JaCoCo hazirligini salt okunur statik plandan raporlar."""

    if project_root is None:
        return {"status": "not-requested", "provider_calls": 0}
    try:
        root = Path(project_root).resolve(strict=True)
        result = build_unit_test_plan(root, target_modules=target_modules)
    except (OSError, ValueError, ZekamError) as exc:
        return {
            "status": "environment-missing",
            "project_root": project_root,
            "reasons": [str(exc)],
            "provider_calls": 0,
        }
    document: dict[str, object] = {
        "status": result.status.value,
        "project_root": str(root),
        "reasons": list(result.reasons),
        "setup_plan": list(result.setup_plan),
        "provider_calls": 0,
    }
    if result.plan is not None:
        document.update(
            {
                "plan_digest": result.plan.plan_digest,
                "execution_class": result.plan.execution_class.value,
                "network_possible": result.plan.network_possible,
                "target_modules": list(result.plan.target_modules),
            }
        )
    return document


def _plan_document(
    request: UnitTestRequest,
    *,
    intent: dict[str, object],
    model: str | None,
    execution: dict[str, object],
    native: bool = False,
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": "zekam-unit-test-plan/v1",
        "request": request.to_payload(),
        "request_digest": request.request_digest,
        "budget": {
            "max_attempts": request.budget.max_attempts,
            "process_timeout_seconds": request.budget.process_timeout_seconds,
            "total_elapsed_seconds": request.budget.total_elapsed_seconds,
        },
        "intent": intent,
        "execution": execution,
        "model_requested": model,
        "provider_calls": 0,
        "provider_free": True,
        "authorization": "not-granted",
        "run_requires": [
            "exact-plan-digest",
            "explicit-run-authorization",
            "local-runtime-ready",
        ],
    }
    if native:
        # Eski plan digest'leri degismesin diye yalniz native planda eklenir.
        document["mode"] = "native-helper"
        document["model_requested"] = None
        document["opencode_required"] = False
        document["run_requires"] = [
            "exact-plan-digest",
            "explicit-measure-authorization",
            "build-approval",
            "local-maven-jacoco-ready",
        ]
    document["plan_digest"] = digest(document)
    return document


@app.command("plan")
def plan_command(
    request_text: Annotated[str | None, typer.Option("--request")] = None,
    project_id: Annotated[str | None, typer.Option("--project-id")] = None,
    source_binding_id: Annotated[str | None, typer.Option("--source-binding-id")] = None,
    source_revision: Annotated[str | None, typer.Option("--source-revision")] = None,
    source: Annotated[list[str] | None, typer.Option("--source")] = None,
    percent: Annotated[str | None, typer.Option("--percent")] = None,
    metric: Annotated[str | None, typer.Option("--metric")] = None,
    policy: Annotated[str | None, typer.Option("--policy")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    max_attempts: Annotated[int, typer.Option("--max-attempts")] = 3,
    process_timeout_seconds: Annotated[int, typer.Option("--process-timeout-seconds")] = 600,
    total_elapsed_seconds: Annotated[int, typer.Option("--total-elapsed-seconds")] = 1800,
    allowed_test_path: Annotated[list[str] | None, typer.Option("--allowed-test-path")] = None,
    forbidden_path: Annotated[list[str] | None, typer.Option("--forbidden-path")] = None,
    project_root: Annotated[str | None, typer.Option("--project-root")] = None,
    target_module: Annotated[list[str] | None, typer.Option("--target-module")] = None,
    work_item_id: Annotated[str | None, typer.Option("--work-item-id")] = None,
    plan_id: Annotated[str | None, typer.Option("--plan-id")] = None,
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    source_snapshot_id: Annotated[str | None, typer.Option("--source-snapshot-id")] = None,
    graph_generation_digest: Annotated[
        str | None, typer.Option("--graph-generation-digest")
    ] = None,
    native: Annotated[
        bool,
        typer.Option(
            "--native",
            help="Native yardimci olcum plani (model/OpenCode yok; `zekam test measure` icin)",
        ),
    ] = False,
) -> None:
    """Exact, provider-free plan uretir; plan yetki veya calistirma baslatmaz."""

    resolved_source = tuple(source or ())
    request, intent = _plan_request(
        request_text=request_text,
        project_id=project_id,
        source_binding_id=source_binding_id,
        source_revision=source_revision,
        source_files=resolved_source,
        percent=percent,
        metric=metric,
        policy=policy,
        max_attempts=max_attempts,
        process_timeout_seconds=process_timeout_seconds,
        total_elapsed_seconds=total_elapsed_seconds,
        allowed_test_paths=tuple(allowed_test_path or ()),
        forbidden_paths=tuple(forbidden_path or ()),
        work_item_id=work_item_id,
        plan_id=plan_id,
        run_id=run_id,
        source_snapshot_id=source_snapshot_id,
        graph_generation_digest=graph_generation_digest,
        native=native,
    )
    _emit(
        _plan_document(
            request,
            intent=intent,
            model=model,
            execution=_execution_plan(
                project_root,
                _native_modules(target_module) if native else tuple(target_module or ()),
            ),
            native=native,
        )
    )


def _read_request(
    request_digest: str,
    *,
    home: str | None,
) -> tuple[object | None, object | None, dict[str, object] | None]:
    context = build_context(home=home)
    store = SQLiteOperationalStore(context.settings.database.sqlite_path(context.home))
    with store.unit_of_work() as uow:
        ledger = uow.unit_test_ledger()
        request = ledger.get_request(request_digest)
        if request is None:
            return None, None, None
        terminals = ledger.list_terminals(request_digest)
        attempts = ledger.list_attempts(request_digest)
        document = {
            "request": request.to_payload(),
            "request_digest": request_digest,
            "attempt_count": len(attempts),
            "unreceipted_attempts": list(ledger.unreceipted_attempts(request_digest)),
            "terminals": [terminal_document(item) for item in terminals],
            "provider_calls": 0,
        }
        return request, terminals, document


@app.command("status")
def status_command(
    request_digest: Annotated[str | None, typer.Argument()] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Ledger statusunu salt okunur gosterir."""

    try:
        context = build_context(home=home)
        store = SQLiteOperationalStore(context.settings.database.sqlite_path(context.home))
        with store.unit_of_work() as uow:
            ledger = uow.unit_test_ledger()
            if request_digest is None:
                rows = ledger.list_requests()
                document = {
                    "schema": "zekam-unit-test-status/v1",
                    "requests": [
                        {"request_digest": digest_value, "created_at": created}
                        for digest_value, _request, created in rows
                    ],
                    "provider_calls": 0,
                }
            else:
                request = ledger.get_request(request_digest)
                if request is None:
                    _finish(
                        gate_document("not-found", reasons=("request-not-found",)),
                        EXIT_NOT_FOUND,
                    )
                terminals = ledger.list_terminals(request_digest)
                document = {
                    "schema": "zekam-unit-test-status/v1",
                    "request_digest": request_digest,
                    "request": request.to_payload(),
                    "attempt_count": len(ledger.list_attempts(request_digest)),
                    "unreceipted_attempts": list(ledger.unreceipted_attempts(request_digest)),
                    "terminals": [terminal_document(item) for item in terminals],
                    "provider_calls": 0,
                }
    except ZekamError as exc:
        _finish(gate_document("environment-missing", reasons=(str(exc),)), EXIT_RUNTIME)
        return
    _emit(document)


@app.command("report")
def report_command(
    request_digest: Annotated[str, typer.Argument()],
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Bir istegin request/attempt/terminal kanit ozetini salt okunur gosterir."""

    try:
        _request, _terminals, document = _read_request(request_digest, home=home)
    except ZekamError as exc:
        _finish(gate_document("environment-missing", reasons=(str(exc),)), EXIT_RUNTIME)
        return
    if document is None:
        _finish(gate_document("not-found", reasons=("request-not-found",)), EXIT_NOT_FOUND)
    document["schema"] = "zekam-unit-test-report/v1"
    _emit(document)


@app.command("run")
def run_command(
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    authorize: Annotated[bool, typer.Option("--authorize")] = False,
    project_id: Annotated[str | None, typer.Option("--project-id")] = None,
    source_binding_id: Annotated[str | None, typer.Option("--source-binding-id")] = None,
    source_revision: Annotated[str | None, typer.Option("--source-revision")] = None,
    source: Annotated[list[str] | None, typer.Option("--source")] = None,
    percent: Annotated[str | None, typer.Option("--percent")] = None,
    metric: Annotated[str | None, typer.Option("--metric")] = None,
    policy: Annotated[str | None, typer.Option("--policy")] = None,
    model: Annotated[str | None, typer.Option("--model")] = None,
    max_attempts: Annotated[int, typer.Option("--max-attempts")] = 3,
    process_timeout_seconds: Annotated[int, typer.Option("--process-timeout-seconds")] = 600,
    total_elapsed_seconds: Annotated[int, typer.Option("--total-elapsed-seconds")] = 1800,
    project_root: Annotated[str | None, typer.Option("--project-root")] = None,
    target_module: Annotated[list[str] | None, typer.Option("--target-module")] = None,
    maven_plan_digest: Annotated[str | None, typer.Option("--maven-plan-digest")] = None,
    realm_id: Annotated[str | None, typer.Option("--realm-id")] = None,
    project_uuid: Annotated[str | None, typer.Option("--project-uuid")] = None,
    work_item_id: Annotated[str | None, typer.Option("--work-item-id")] = None,
    coordinator_assignment_id: Annotated[
        str | None, typer.Option("--coordinator-assignment-id")
    ] = None,
    plan_id: Annotated[str | None, typer.Option("--plan-id")] = None,
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    source_snapshot_id: Annotated[str | None, typer.Option("--source-snapshot-id")] = None,
    graph_generation_digest: Annotated[
        str | None, typer.Option("--graph-generation-digest")
    ] = None,
    step_id: Annotated[str | None, typer.Option("--step-id")] = None,
    write_tests: Annotated[bool, typer.Option("--write-tests")] = False,
    build: Annotated[bool, typer.Option("--build")] = False,
    remote_model: Annotated[bool, typer.Option("--remote-model")] = False,
    allow_network: Annotated[bool, typer.Option("--allow-network")] = False,
    max_remote_calls: Annotated[int, typer.Option("--max-remote-calls")] = 0,
    resume: Annotated[bool, typer.Option("--resume")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Exact plan/yetki/baglarla gercek local loop'u calistirir."""

    if not plan_digest:
        _finish(
            gate_document("policy-violation", reasons=("exact-plan-digest-missing",)),
            EXIT_POLICY,
        )
    if not authorize:
        _finish(
            gate_document("policy-violation", reasons=("explicit-run-authorization-missing",)),
            EXIT_POLICY,
        )
    if not project_root or not maven_plan_digest:
        _finish(
            gate_document("environment-missing", reasons=("project-root-or-maven-plan-missing",)),
            EXIT_ENVIRONMENT_MISSING,
        )
    if not source_snapshot_id or not graph_generation_digest:
        _finish(
            gate_document(
                "policy-violation",
                reasons=("source-snapshot-and-graph-generation-binding-missing",),
            ),
            EXIT_POLICY,
        )
    request, intent = _plan_request(
        request_text=None,
        project_id=project_id,
        source_binding_id=source_binding_id,
        source_revision=source_revision,
        source_files=tuple(source or ()),
        percent=percent,
        metric=metric,
        policy=policy,
        max_attempts=max_attempts,
        process_timeout_seconds=process_timeout_seconds,
        total_elapsed_seconds=total_elapsed_seconds,
        allowed_test_paths=(),
        forbidden_paths=(),
        work_item_id=work_item_id,
        plan_id=plan_id,
        run_id=run_id,
        source_snapshot_id=source_snapshot_id,
        graph_generation_digest=graph_generation_digest,
    )
    execution = _execution_plan(project_root, tuple(target_module or ()))
    expected = _plan_document(request, intent=intent, model=model, execution=execution)
    if expected["plan_digest"] != plan_digest:
        _finish(
            gate_document("policy-violation", reasons=("exact-plan-digest-mismatch",)),
            EXIT_POLICY,
        )
    if execution.get("status") != "ready":
        _finish(
            gate_document(
                "environment-missing",
                reasons=tuple(str(item) for item in execution.get("reasons", ()))
                or ("maven-plan-not-ready",),
            ),
            EXIT_ENVIRONMENT_MISSING,
        )
    try:
        parsed_realm = UUID(realm_id or "")
        parsed_project = UUID(project_uuid or "")
        parsed_work = UUID(work_item_id or "")
        parsed_coordinator = UUID(coordinator_assignment_id or "")
        parsed_plan = UUID(plan_id) if plan_id else None
        parse_digest(maven_plan_digest or "")  # bicim dogrulamasi; parse hex doner
        parsed_maven_digest = maven_plan_digest or ""
        if project_id != str(parsed_project):
            raise ValueError("project-id ile project-uuid ayni exact bag olmali")
        executable = _resolve_opencode_executable()
        context = build_context(home=home)
        store = SQLiteOperationalStore(context.settings.database.sqlite_path(context.home))
        binding = UnitTestRuntimeBinding(
            realm_id=parsed_realm,
            project_id=parsed_project,
            work_item_id=parsed_work,
            coordinator_assignment_id=parsed_coordinator,
            project_root=Path(project_root).resolve(strict=True),
            object_store_root=(context.home / context.settings.object_store_relative).resolve(),
            lock_dir=(context.home / "runtime" / "unit-test-locks").resolve(),
            opencode_executable=executable,
            approved_maven_plan_digest=parsed_maven_digest,
            target_modules=tuple(target_module or ()),
            allow_network=allow_network,
            remote_model=remote_model,
            model_id=model,
            plan_id=parsed_plan,
            step_id=step_id,
        )
        approvals = UnitTestApprovals(
            write_tests=write_tests,
            build=build,
            network=allow_network,
            remote_model=remote_model,
            approved_budget=request.budget,
            approved_limits=LoopLimits(max_remote_calls=max_remote_calls),
        )
        limits = LoopLimits(max_remote_calls=max_remote_calls)
        with store.unit_of_work() as uow:
            uow.assert_unit_test_bindings(
                project_id=str(parsed_project),
                realm_id=str(parsed_realm),
                work_item_id=str(parsed_work),
                source_binding_id=request.source_binding_id,
                source_snapshot_id=source_snapshot_id,
                source_revision=request.source_revision,
                run_id=run_id,
            )
            project_record = uow.resolve_project(str(parsed_project))
            graph_path = graph_store_path(context.home, project_record.slug)
            with SQLiteCodeGraphStore(graph_path, read_only=True) as graph:
                generation = graph.generation(str(parsed_project))
                if (
                    generation.state != "ready"
                    or generation.generation_digest != graph_generation_digest
                    or generation.source_revision != request.source_revision
                ):
                    raise ValueError("graph generation stale or request source revision mismatch")
            ledger = uow.unit_test_ledger()

            def register_artifact(artifact_digest: str, size_bytes: int, media_type: str) -> None:
                uow.register_artifact(
                    artifact_digest=artifact_digest,
                    media_type=media_type,
                    size_bytes=size_bytes,
                    classification="local-private",
                )

            runtime = compose_unit_test_runtime(
                request,
                ledger=ledger,
                binding=binding,
                artifact_registrar=register_artifact,
                control=LedgerLoopControl(
                    ledger,
                    request.request_digest,
                    allow_resume=resume,
                ),
            )
            outcome = runtime.loop.run(request, approvals=approvals, limits=limits)
            uow.commit()
    except (OSError, TypeError, ValueError, ZekamError) as exc:
        _finish(gate_document("environment-missing", reasons=(str(exc),)), EXIT_ENVIRONMENT_MISSING)
        return
    document = {
        "schema": "zekam-unit-test-run/v1",
        "request_digest": outcome.request_digest,
        "terminal": terminal_document(outcome.terminal),
        "attempts": outcome.attempts,
        "executed_attempts": outcome.executed_attempts,
        "detail": list(outcome.detail),
        "provider_calls": 0 if not runtime.gateway.remote else max_remote_calls,
    }
    _emit(document)
    code = exit_code_for_reason(outcome.terminal.stop_reason)
    if code:
        raise typer.Exit(code)


def _native_modules(target_module: list[str] | None) -> tuple[str, ...]:
    """Native olcumde hedef modul verilmezse kok modul (``""``); bos tuple olcum uretemez."""

    return tuple(target_module) if target_module else ("",)


def _reasons(execution: dict[str, object]) -> list[object]:
    value = execution.get("reasons", ())
    return list(value) if isinstance(value, (list, tuple)) else []


def _assert_operational_bindings(
    uow: Any,
    *,
    context: Any,
    request: UnitTestRequest,
    project_uuid: UUID,
    realm_id: UUID,
    work_item_id: UUID,
    source_snapshot_id: str,
    graph_generation_digest: str,
    run_id: str | None,
) -> None:
    """Exact operational + graph generation baglari; measure bu kapiyi kullanir."""

    uow.assert_unit_test_bindings(
        project_id=str(project_uuid),
        realm_id=str(realm_id),
        work_item_id=str(work_item_id),
        source_binding_id=request.source_binding_id,
        source_snapshot_id=source_snapshot_id,
        source_revision=request.source_revision,
        run_id=run_id,
    )
    project_record = uow.resolve_project(str(project_uuid))
    graph_path = graph_store_path(context.home, project_record.slug)
    with SQLiteCodeGraphStore(graph_path, read_only=True) as graph:
        generation = graph.generation(str(project_uuid))
        if (
            generation.state != "ready"
            or generation.generation_digest != graph_generation_digest
            or generation.source_revision != request.source_revision
        ):
            raise ValueError("graph generation stale or request source revision mismatch")


@app.command("measure")
def measure_command(
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    authorize: Annotated[bool, typer.Option("--authorize")] = False,
    project_id: Annotated[str | None, typer.Option("--project-id")] = None,
    source_binding_id: Annotated[str | None, typer.Option("--source-binding-id")] = None,
    source_revision: Annotated[str | None, typer.Option("--source-revision")] = None,
    source: Annotated[list[str] | None, typer.Option("--source")] = None,
    percent: Annotated[str | None, typer.Option("--percent")] = None,
    metric: Annotated[str | None, typer.Option("--metric")] = None,
    policy: Annotated[str | None, typer.Option("--policy")] = None,
    max_attempts: Annotated[int, typer.Option("--max-attempts")] = 3,
    process_timeout_seconds: Annotated[int, typer.Option("--process-timeout-seconds")] = 600,
    total_elapsed_seconds: Annotated[int, typer.Option("--total-elapsed-seconds")] = 1800,
    allowed_test_path: Annotated[list[str] | None, typer.Option("--allowed-test-path")] = None,
    forbidden_path: Annotated[list[str] | None, typer.Option("--forbidden-path")] = None,
    project_root: Annotated[str | None, typer.Option("--project-root")] = None,
    target_module: Annotated[list[str] | None, typer.Option("--target-module")] = None,
    maven_plan_digest: Annotated[str | None, typer.Option("--maven-plan-digest")] = None,
    realm_id: Annotated[str | None, typer.Option("--realm-id")] = None,
    project_uuid: Annotated[str | None, typer.Option("--project-uuid")] = None,
    work_item_id: Annotated[str | None, typer.Option("--work-item-id")] = None,
    plan_id: Annotated[str | None, typer.Option("--plan-id")] = None,
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    source_snapshot_id: Annotated[str | None, typer.Option("--source-snapshot-id")] = None,
    graph_generation_digest: Annotated[
        str | None, typer.Option("--graph-generation-digest")
    ] = None,
    build: Annotated[bool, typer.Option("--build")] = False,
    allow_network: Annotated[bool, typer.Option("--allow-network")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Native yardimci olcum: gercek Maven/JaCoCo olcer, kaniti ledger'a yazar; model baslatmaz.

    Native ajan testi kendi yazar; bu komut `plan --native` ciktisindaki exact plan digest'i,
    acik yetki (`--authorize --build`) ve exact Work/source baglariyla taze olcum alir.
    Her cagri onayli butce (`--max-attempts`) icinde yeni bir kontrollu tekrar olcumdur.
    OpenCode executable'i gerekmez; model/provider cagrisi yapilmaz.
    """

    if not plan_digest:
        _finish(
            gate_document("policy-violation", reasons=("exact-plan-digest-missing",)),
            EXIT_POLICY,
        )
    if not authorize:
        _finish(
            gate_document("policy-violation", reasons=("explicit-measure-authorization-missing",)),
            EXIT_POLICY,
        )
    if not build:
        _finish(
            gate_document("policy-violation", reasons=("build-approval-missing",)),
            EXIT_POLICY,
        )
    if not project_root or not maven_plan_digest:
        _finish(
            gate_document("environment-missing", reasons=("project-root-or-maven-plan-missing",)),
            EXIT_ENVIRONMENT_MISSING,
        )
    if not source_snapshot_id or not graph_generation_digest:
        _finish(
            gate_document(
                "policy-violation",
                reasons=("source-snapshot-and-graph-generation-binding-missing",),
            ),
            EXIT_POLICY,
        )
    request, intent = _plan_request(
        request_text=None,
        project_id=project_id,
        source_binding_id=source_binding_id,
        source_revision=source_revision,
        source_files=tuple(source or ()),
        percent=percent,
        metric=metric,
        policy=policy,
        max_attempts=max_attempts,
        process_timeout_seconds=process_timeout_seconds,
        total_elapsed_seconds=total_elapsed_seconds,
        allowed_test_paths=tuple(allowed_test_path or ()),
        forbidden_paths=tuple(forbidden_path or ()),
        work_item_id=work_item_id,
        plan_id=plan_id,
        run_id=run_id,
        source_snapshot_id=source_snapshot_id,
        graph_generation_digest=graph_generation_digest,
        native=True,
    )
    modules = _native_modules(target_module)
    execution = _execution_plan(project_root, modules)
    expected = _plan_document(request, intent=intent, model=None, execution=execution, native=True)
    if expected["plan_digest"] != plan_digest:
        _finish(
            gate_document("policy-violation", reasons=("exact-plan-digest-mismatch",)),
            EXIT_POLICY,
        )
    if execution.get("status") != "ready":
        _finish(
            gate_document(
                "environment-missing",
                reasons=tuple(str(item) for item in _reasons(execution))
                or ("maven-plan-not-ready",),
            ),
            EXIT_ENVIRONMENT_MISSING,
        )
    if execution.get("network_possible") and not allow_network:
        _finish(
            gate_document(
                "policy-violation",
                reasons=("network-authorization-missing",),
                next_steps=(
                    "Maven bagimlilik/plugin cozumleyebilir; --allow-network ile acik yetki verin.",
                ),
            ),
            EXIT_POLICY,
        )
    try:
        parsed_realm = UUID(realm_id or "")
        parsed_project = UUID(project_uuid or "")
        parsed_work = UUID(work_item_id or "")
        parse_digest(maven_plan_digest or "")  # bicim dogrulamasi; parse hex doner
        parsed_maven_digest = maven_plan_digest or ""
        if project_id != str(parsed_project):
            raise ValueError("project-id ile project-uuid ayni exact bag olmali")
        if parsed_maven_digest != execution.get("plan_digest"):
            raise ValueError("maven-plan-digest hazir Maven planiyla ayni exact digest olmali")
        context = build_context(home=home)
        store = SQLiteOperationalStore(context.settings.database.sqlite_path(context.home))
        binding = MavenMeasurementBinding(
            project_root=Path(project_root or "").resolve(strict=True),
            object_store_root=(context.home / context.settings.object_store_relative).resolve(),
            lock_dir=(context.home / "runtime" / "unit-test-locks").resolve(),
            approved_maven_plan_digest=parsed_maven_digest,
            target_modules=modules,
            allow_network=allow_network,
        )
        with store.unit_of_work() as uow:
            _assert_operational_bindings(
                uow,
                context=context,
                request=request,
                project_uuid=parsed_project,
                realm_id=parsed_realm,
                work_item_id=parsed_work,
                source_snapshot_id=source_snapshot_id or "",
                graph_generation_digest=graph_generation_digest or "",
                run_id=run_id,
            )
            uow.rollback()  # yalniz okuma kapisi; Maven calisirken kilit tutulmaz
        result = measure_native(
            request, compose_native_measurement(request, store=store, binding=binding)
        )
    except (OSError, TypeError, ValueError, ZekamError) as exc:
        _finish(gate_document("environment-missing", reasons=(str(exc),)), EXIT_ENVIRONMENT_MISSING)
        return
    _emit(result.to_document())
    if result.process_exit_code:
        raise typer.Exit(result.process_exit_code)


def _evidence_summary(
    objects: LocalContentAddressedStore, evidence_digest: str
) -> dict[str, object]:
    """Kanit nesnesini digest dogrulamasiyla okur; bozuk/eksik kanit gorunur kalir."""

    try:
        loaded = json.loads(objects.get(evidence_digest).decode("utf-8"))
    except (OSError, ValueError, ZekamError) as exc:
        return {"verified": False, "reason": type(exc).__name__}
    if not isinstance(loaded, dict):
        return {"verified": False, "reason": "not-an-object"}
    summary: dict[str, object] = {"verified": True, "contract": loaded.get("contract")}
    if loaded.get("contract") == NATIVE_EVIDENCE_CONTRACT:
        inner = loaded.get("evidence", {})
        if isinstance(inner, dict):
            measurement = inner.get("measurement") or {}
            summary.update(
                {
                    "outcome": inner.get("outcome"),
                    "freshness": inner.get("freshness"),
                    "drift": inner.get("drift"),
                    "failed_cases": inner.get("failed_cases"),
                    "measurement_status": measurement.get("status"),
                    "build_exit_code": measurement.get("exit_code"),
                    "test_counts": measurement.get("test_counts"),
                    "test_tree_digest_before": inner.get("test_tree_digest_before"),
                    "binding_digest": measurement.get("binding_digest"),
                }
            )
    return summary


@app.command("evidence")
def evidence_command(
    request_digest: Annotated[str, typer.Argument()],
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Attempt receipt ve kanit nesnelerini digest dogrulamasiyla salt okunur gosterir."""

    try:
        context = build_context(home=home)
        store = SQLiteOperationalStore(context.settings.database.sqlite_path(context.home))
        objects = LocalContentAddressedStore(
            (context.home / context.settings.object_store_relative).resolve()
        )
        with store.unit_of_work() as uow:
            ledger = uow.unit_test_ledger()
            request = ledger.get_request(request_digest)
            if request is None:
                _finish(gate_document("not-found", reasons=("request-not-found",)), EXIT_NOT_FOUND)
                return
            attempts = []
            for attempt in ledger.list_attempts(request_digest):
                receipt = ledger.get_attempt_receipt(attempt.attempt_id)
                attempts.append(
                    {
                        "attempt_id": attempt.attempt_id,
                        "ordinal": attempt.ordinal,
                        "candidate_digest": attempt.candidate_digest,
                        "plan_digest": attempt.plan_digest,
                        "receipt": (
                            None
                            if receipt is None
                            else {
                                "status": receipt.status.value,
                                "evidence_digest": receipt.evidence_digest,
                                "observations": [
                                    item.to_payload()
                                    for item in ledger.list_observations(attempt.attempt_id)
                                ],
                                "evidence": _evidence_summary(objects, receipt.evidence_digest),
                            }
                        ),
                    }
                )
            document = {
                "schema": "zekam-unit-test-evidence/v1",
                "request_digest": request_digest,
                "native_request": NATIVE_MODE_MARKER in request.defaults_applied,
                "attempts": attempts,
                "unreceipted_attempts": list(ledger.unreceipted_attempts(request_digest)),
                "terminals": [
                    terminal_document(item) for item in ledger.list_terminals(request_digest)
                ],
                "provider_calls": 0,
            }
    except ZekamError as exc:
        _finish(gate_document("environment-missing", reasons=(str(exc),)), EXIT_RUNTIME)
        return
    _emit(document)


def _resolve_opencode_executable() -> Path:
    found = shutil.which("opencode")
    if found is None:
        raise OSError("OpenCode executable PATH'te yok")
    path = Path(found).resolve(strict=True)
    if not path.is_file():
        raise OSError("OpenCode executable regular dosya degil")
    return path


def _record_control(
    action: str,
    request_digest: str | None,
    *,
    home: str | None,
) -> None:
    """Pause/cancel'i ayni operational ledger'e durable terminal olarak yazar."""

    if not request_digest:
        _finish(
            gate_document("usage-error", reasons=("request-digest-missing",)),
            EXIT_USAGE,
        )
    try:
        context = build_context(home=home)
        store = SQLiteOperationalStore(context.settings.database.sqlite_path(context.home))
        with store.unit_of_work() as uow:
            ledger = uow.unit_test_ledger()
            request = ledger.get_request(request_digest)
            if request is None:
                _finish(
                    gate_document("not-found", reasons=("request-not-found",)),
                    EXIT_NOT_FOUND,
                )
            if action == "resume":
                terminals = ledger.list_terminals(request_digest)
                if terminals and terminals[-1].stop_reason is UnitTestStopReason.USER_CANCELLED:
                    _finish(
                        gate_document(
                            "policy-violation",
                            reasons=("cancelled-request-cannot-resume",),
                        ),
                        EXIT_POLICY,
                    )
                _emit(
                    {
                        "schema": "zekam-unit-test-control/v1",
                        "action": "resume",
                        "request_digest": request_digest,
                        "status": "ready",
                        "next_steps": [
                            "Ayni exact plan baglariyla zekam test run --resume calistirin."
                        ],
                        "provider_calls": 0,
                    }
                )
                return
            reason = (
                UnitTestStopReason.USER_PAUSED
                if action == "pause"
                else UnitTestStopReason.USER_CANCELLED
            )
            terminal = UnitTestTerminal(request_digest, reason, None)
            ledger.record_terminal(terminal, now=datetime.now(UTC))
            uow.commit()
    except (OSError, TypeError, ValueError, ZekamError) as exc:
        _finish(gate_document("environment-missing", reasons=(str(exc),)), EXIT_ENVIRONMENT_MISSING)
        return
    _emit(
        {
            "schema": "zekam-unit-test-control/v1",
            "action": action,
            "request_digest": request_digest,
            "status": "recorded",
            "terminal": terminal_document(terminal),
            "provider_calls": 0,
        }
    )


@app.command("pause")
def pause_command(
    request_digest: Annotated[str | None, typer.Argument()] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Pause istegini operational ledger'e durable terminal olarak yazar."""

    _record_control("pause", request_digest, home=home)


@app.command("resume")
def resume_command(
    request_digest: Annotated[str | None, typer.Argument()] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Resume icin exact run baglarini yeniden kullanmaya hazirlik verir."""

    _record_control("resume", request_digest, home=home)


@app.command("cancel")
def cancel_command(
    request_digest: Annotated[str | None, typer.Argument()] = None,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Cancel istegini operational ledger'e final terminal olarak yazar."""

    _record_control("cancel", request_digest, home=home)
