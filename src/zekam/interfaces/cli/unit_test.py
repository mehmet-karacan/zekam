"""Provider-free ``zekam test`` CLI ve ledger okuma yuzeyi (W07)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from zekam.application.composition import build_context
from zekam.application.unit_test_intent import (
    UnitTestIntentKind,
    classify_unit_test_request,
)
from zekam.application.unit_test_outcome import (
    EXIT_CLARIFICATION,
    EXIT_ENVIRONMENT_MISSING,
    EXIT_NOT_FOUND,
    EXIT_POLICY,
    EXIT_RUNTIME,
    EXIT_USAGE,
    gate_document,
    terminal_document,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ZekamError
from zekam.domain.unit_test_engineering import (
    CoverageMetric,
    CoveragePolicy,
    UnitTestBudget,
    UnitTestRequest,
)
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
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
    return request, intent_document


def _execution_plan(
    project_root: str | None, target_modules: tuple[str, ...]
) -> dict[str, object]:
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
    )
    plan_payload: dict[str, object] = {
        "schema": "zekam-unit-test-plan/v1",
        "request": request.to_payload(),
        "request_digest": request.request_digest,
        "budget": {
            "max_attempts": request.budget.max_attempts,
            "process_timeout_seconds": request.budget.process_timeout_seconds,
            "total_elapsed_seconds": request.budget.total_elapsed_seconds,
        },
        "intent": intent,
        "execution": _execution_plan(project_root, tuple(target_module or ())),
        "model_requested": model,
        "provider_calls": 0,
        "provider_free": True,
        "authorization": "not-granted",
        "run_requires": ["exact-plan-digest", "explicit-run-authorization", "local-runtime-ready"],
    }
    plan_payload["plan_digest"] = digest(plan_payload)
    _emit(plan_payload)


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


def _control_gate(action: str, request_digest: str | None) -> None:
    if not request_digest:
        _finish(
            gate_document("usage-error", reasons=("request-digest-missing",)),
            EXIT_USAGE,
        )
    _finish(
        gate_document(
            "setup-required",
            reasons=(f"{action}-durable-control-not-composed",),
            next_steps=("LoopControl ile operational control kaydini baglayin.",),
        ),
        EXIT_ENVIRONMENT_MISSING,
    )


@app.command("run")
def run_command(
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    authorize: Annotated[bool, typer.Option("--authorize")] = False,
) -> None:
    """Calistirma kapisi; runtime composition tamamlanmadan effect baslatmaz."""

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
    _finish(
        gate_document(
            "setup-required",
            reasons=("unit-test-runtime-composition-not-ready",),
            next_steps=(
                "Maven/JaCoCo + canonical agent gateway + durable control compositionini "
                "tamamlayin.",
            ),
        ),
        EXIT_ENVIRONMENT_MISSING,
    )


@app.command("pause")
def pause_command(request_digest: Annotated[str | None, typer.Argument()] = None) -> None:
    """Pause kapisini fail-closed tutar; mevcut CLI sahte control yazmaz."""

    _control_gate("pause", request_digest)


@app.command("resume")
def resume_command(request_digest: Annotated[str | None, typer.Argument()] = None) -> None:
    """Resume kapisini fail-closed tutar; mevcut CLI sahte calistirma yapmaz."""

    _control_gate("resume", request_digest)


@app.command("cancel")
def cancel_command(request_digest: Annotated[str | None, typer.Argument()] = None) -> None:
    """Cancel kapisini fail-closed tutar; mevcut CLI sahte terminal yazmaz."""

    _control_gate("cancel", request_digest)
