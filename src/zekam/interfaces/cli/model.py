"""Provider-free local model compatibility commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from zekam.application.home import resolve_home
from zekam.application.model_benchmark_service import load_fixture_registry
from zekam.application.native_benchmark_campaign import (
    build_native_campaign,
    run_native_campaign,
)
from zekam.application.portable_benchmark import inspect_portable_benchmark
from zekam.domain.canonical import parse_digest
from zekam.domain.errors import ZekamError
from zekam.infrastructure.sqlite.local_model_benchmark import SQLiteLocalBenchmarkLab

app = typer.Typer(name="model", help="Yerel model kanit ve benchmark yuzeyi")
campaign_app = typer.Typer(
    name="campaign",
    help="Provider-free native benchmark kampanyasi",
    no_args_is_help=True,
)
app.add_typer(campaign_app)
console = Console()
error_console = Console(stderr=True)


def _campaign_lab(home: str | None) -> SQLiteLocalBenchmarkLab:
    root = resolve_home(home)
    return SQLiteLocalBenchmarkLab(
        (root / "benchmarklar" / "benchmark.db").absolute(),
        (root / "benchmarklar" / "artifacts").absolute(),
    )


def _emit(document: dict[str, object], *, output_json: bool, summary: str) -> None:
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(summary)


@app.command("benchmark")
def benchmark_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    model: Annotated[str | None, typer.Option("--model")] = None,
    inventory_digest: Annotated[str | None, typer.Option("--inventory-digest")] = None,
    policy_digest: Annotated[str | None, typer.Option("--policy-digest")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
) -> None:
    if apply or any(value is not None for value in (model, inventory_digest, policy_digest)):
        error_console.print("Benchmark apply exact authorization runtime gate ister")
        raise typer.Exit(6)
    registry = load_fixture_registry()
    document = {
        "schema": "zekam-local-benchmark-catalog/v1",
        "fixture_count": len(registry.fixtures),
        "local_fixture_count": len(registry.eligible(remote=False)),
        "remote_fixture_count": len(registry.eligible(remote=True)),
        "registry_digest": registry.registry_digest,
        "provider_calls": 0,
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        console.print_json(json.dumps(document, sort_keys=True))
        return
    console.print(f"fixtures={len(registry.fixtures)} provider_calls=0")


@app.command("decide")
def decide_command(
    input_path: Annotated[str, typer.Option("--girdi")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    del input_path, output_json
    error_console.print("Caller-supplied model candidates cannot satisfy authoritative hard gate")
    raise typer.Exit(6)


@app.command("health")
def health_command(
    apply: Annotated[bool, typer.Option("--uygula")] = False,
) -> None:
    if apply:
        error_console.print(
            "Production health sentetik probe ile yazilamaz; exact authorization gerekir"
        )
        raise typer.Exit(6)
    console.print("Dry-run; provider call yok")


@app.command("portable-inspect")
def portable_inspect_command(
    root: Annotated[str, typer.Option("--root")],
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Inspect a portable benchmark catalog without executing its code."""

    try:
        document = inspect_portable_benchmark(Path(root))
    except ZekamError as exc:
        error_console.print(f"Hata: {exc}")
        raise typer.Exit(70) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
        return
    models = document["models"]
    tasks = document["tasks"]
    console.print(
        f"models={models['total']} real={models['real']} tasks={tasks['total']} provider_calls=0"
    )


@campaign_app.command("plan")
def campaign_plan_command(
    repetitions: Annotated[int, typer.Option("--repetitions", min=5, max=100)] = 5,
    portable_root: Annotated[Path | None, typer.Option("--portable-root")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Build an exact provider-free pipeline acceptance plan without mutation."""

    try:
        document = build_native_campaign(
            repetitions=repetitions, portable_root=portable_root
        ).plan_document()
    except ZekamError as exc:
        error_console.print(f"Hata: {exc}")
        raise typer.Exit(70) from exc
    _emit(
        document,
        output_json=output_json,
        summary=(
            f"plan={document['plan_digest']} trials={document['trial_count']} "
            f"calls={document['exact_call_budget']} provider_calls=0"
        ),
    )


@campaign_app.command("run")
def campaign_run_command(
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    repetitions: Annotated[int, typer.Option("--repetitions", min=5, max=100)] = 5,
    portable_root: Annotated[Path | None, typer.Option("--portable-root")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Execute/replay one exact provider-free campaign in the durable ledger."""

    try:
        contracts = build_native_campaign(repetitions=repetitions, portable_root=portable_root)
        expected = contracts.plan.plan_digest
        if not apply:
            raise ZekamError("Campaign run --uygula ister")
        if plan_digest is None:
            raise ZekamError("Campaign run exact --plan-digest ister")
        parse_digest(plan_digest)
        if plan_digest != expected:
            raise ZekamError("Campaign plan digest stale veya farkli")
        lab = _campaign_lab(home)
        if not lab.path.exists():
            lab.bootstrap()
        else:
            lab.prepare_local_security()
        document = run_native_campaign(lab, contracts)
    except ZekamError as exc:
        error_console.print(f"Hata: {exc}")
        raise typer.Exit(70) from exc
    _emit(
        document,
        output_json=output_json,
        summary=(
            f"state={document['state']} trials={document['trial_count']} "
            f"new_claims={document['new_claims']} provider_calls=0"
        ),
    )


def _campaign_snapshot(
    *,
    home: str | None,
    repetitions: int,
    plan_digest: str | None,
    portable_root: Path | None,
) -> tuple[dict[str, object], str]:
    contracts = build_native_campaign(repetitions=repetitions, portable_root=portable_root)
    expected = contracts.plan.plan_digest
    selected = expected if plan_digest is None else plan_digest
    parse_digest(selected)
    if selected != expected:
        raise ZekamError("Campaign plan digest current native plan ile eslesmiyor")
    lab = _campaign_lab(home)
    if not lab.path.is_file():
        return (
            {
                "schema": "zekam-native-benchmark-campaign-status/v1",
                "plan_digest": selected,
                "state": "not-found",
                "provider_calls": 0,
                "read_only": True,
                "grants_authority": False,
            },
            selected,
        )
    snapshot = lab.campaign_snapshot(selected)
    if snapshot is None:
        return (
            {
                "schema": "zekam-native-benchmark-campaign-status/v1",
                "plan_digest": selected,
                "state": "not-found",
                "provider_calls": 0,
                "read_only": True,
                "grants_authority": False,
            },
            selected,
        )
    return snapshot, selected


@campaign_app.command("status")
def campaign_status_command(
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    repetitions: Annotated[int, typer.Option("--repetitions", min=5, max=100)] = 5,
    portable_root: Annotated[Path | None, typer.Option("--portable-root")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Read one exact campaign's durable progress without mutation."""

    try:
        document, _selected = _campaign_snapshot(
            home=home,
            repetitions=repetitions,
            plan_digest=plan_digest,
            portable_root=portable_root,
        )
    except ZekamError as exc:
        error_console.print(f"Hata: {exc}")
        raise typer.Exit(70) from exc
    _emit(
        document,
        output_json=output_json,
        summary=f"state={document['state']} plan={document['plan_digest']}",
    )


@campaign_app.command("report")
def campaign_report_command(
    plan_digest: Annotated[str | None, typer.Option("--plan-digest")] = None,
    repetitions: Annotated[int, typer.Option("--repetitions", min=5, max=100)] = 5,
    portable_root: Annotated[Path | None, typer.Option("--portable-root")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    home: Annotated[str | None, typer.Option("--home")] = None,
) -> None:
    """Read aggregate evidence while preserving its acceptance-only meaning."""

    try:
        status, selected = _campaign_snapshot(
            home=home,
            repetitions=repetitions,
            plan_digest=plan_digest,
            portable_root=portable_root,
        )
    except ZekamError as exc:
        error_console.print(f"Hata: {exc}")
        raise typer.Exit(70) from exc
    aggregate = status.get("aggregate")
    document: dict[str, object] = {
        "schema": "zekam-native-benchmark-campaign-report/v1",
        "campaign_kind": "pipeline-acceptance",
        "plan_digest": selected,
        "state": status["state"],
        "report_ready": aggregate is not None,
        "qualifies_production_models": False,
        "provider_calls": 0,
        "aggregate": aggregate,
        "read_only": True,
        "grants_authority": False,
    }
    _emit(
        document,
        output_json=output_json,
        summary=f"state={document['state']} report_ready={document['report_ready']}",
    )
