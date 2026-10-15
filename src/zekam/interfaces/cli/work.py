"""Local operational work graph commands."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from zekam.application.bounded_file import read_bounded_bytes
from zekam.application.operational_store import OperationalWorkRecord
from zekam.domain.canonical import digest
from zekam.domain.errors import ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.work import OPEN_STATES, WorkState, WorkType
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, fail_from, sqlite_operational_store

app = typer.Typer(name="work", help="Yerel work graph", no_args_is_help=True)
console = Console()
_MAX_ACTIVE_SPEC_BYTES = 1_048_576
_MAX_JSON_ARGUMENT_BYTES = 262_144
_RESUME_WORK_LIMIT = 100


@app.command("resume")
def resume_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    project: Annotated[str | None, typer.Option("--project", "--proje")] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Read the canonical local work graph without granting authority."""

    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            project_id = uow.resolve_project(project).id if project is not None else None
            items = uow.list_work_limited(
                limit=_RESUME_WORK_LIMIT + 1,
                project_id=project_id,
                states=tuple(sorted(OPEN_STATES)),
            )
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    open_items = [
        {
            "id": item.id,
            "project_id": item.project_id,
            "type": item.kind,
            "state": item.state,
            "title": item.title,
            "revision": item.revision,
        }
        for item in items[:_RESUME_WORK_LIMIT]
    ]
    document = {
        "source": "sqlite-work-graph",
        "project_id": project_id,
        "open_items": open_items,
        "blocked": [item for item in open_items if item["state"] == "blocked"],
        "recent_activity": [],
        "next_safe_action": "ready work item sec" if open_items else "acik is bulunmuyor",
        "read_only": True,
        "grants_authority": False,
        "bounded": True,
        "work_limit": _RESUME_WORK_LIMIT,
        "has_more": len(items) > _RESUME_WORK_LIMIT,
    }
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
        return
    console.print(f"acik is: {len(open_items)}")


def _read_active_spec(path: Path, expected_digest: str) -> tuple[str, tuple[str, ...], str]:
    try:
        raw = read_bounded_bytes(path, max_bytes=_MAX_ACTIVE_SPEC_BYTES)
    except OSError as exc:
        raise ZekamError("Aktif gorev spec dosyasi okunamadi") from exc
    if not raw or len(raw) > _MAX_ACTIVE_SPEC_BYTES:
        raise ZekamError("Aktif gorev spec dosyasi bounded boyutta olmali")
    actual_digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if expected_digest.strip().lower() != actual_digest:
        raise ZekamError("Aktif gorev spec SHA-256 drift")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ZekamError("Aktif gorev spec UTF-8 olmali") from exc
    if not lines or not lines[0].startswith("# "):
        raise ZekamError("Aktif gorev spec H1 basligi ister")
    title = lines[0][2:].strip().split("—", 1)[-1].strip()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("## ")
            and any(
                label in line.casefold()
                for label in ("tamamlanma ölçütleri", "zorunlu kabul kriterleri")
            )
        ),
        None,
    )
    if start is None:
        raise ZekamError("Aktif gorev spec tamamlanma olcutleri bolumu ister")
    criteria: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        match = re.fullmatch(r"- \[[ xX]\] (.+)", line.strip())
        if match:
            criteria.append(match.group(1).strip())
    if not title or not criteria or len(criteria) != len(set(criteria)):
        raise ZekamError("Aktif gorev spec title ve tekil kabul kriterleri ister")
    return title, tuple(criteria), actual_digest


@app.command("create")
def create_command(
    project: Annotated[str, typer.Argument()],
    title: Annotated[str, typer.Argument()],
    work_type: Annotated[WorkType, typer.Option("--tur")] = WorkType.TASK,
    summary: Annotated[str, typer.Option("--ozet")] = "",
    number: Annotated[str | None, typer.Option("--numara")] = None,
    criterion: Annotated[list[str] | None, typer.Option("--kriter")] = None,
    idempotency_key: Annotated[
        str | None, typer.Option("--key", "--idempotency-key", help="Tekrarlanan exact istek kimligi")
    ] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    if not apply:
        console.print_json(json.dumps({"title": title, "apply": False}))
        return
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            project_row = uow.resolve_project(project)
            item = uow.create_work(
                project_id=project_row.id,
                kind=work_type.value,
                title=title,
                state=WorkState.PROPOSED.value,
                payload={"summary": summary, "acceptance_criteria": list(criterion or ())},
                external_number=number,
                idempotency_key=idempotency_key,
            )
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps({"work": _work_document(item), "apply": True}))
    elif idempotency_key is not None:
        console.print(f"Kayit: {item.id} ({item.state})")
    else:
        console.print(f"[green]Olusturuldu:[/green] {item.id} ({item.state})")


@app.command("list")
def list_command(
    project: Annotated[str | None, typer.Option("--proje")] = None,
    state: Annotated[list[WorkState] | None, typer.Option("--durum")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            project_id = uow.resolve_project(project).id if project else None
            allowed = {item.value for item in state or ()}
            rows = [
                {
                    "id": item.id,
                    "external_number": item.external_number,
                    "type": item.kind,
                    "state": item.state,
                    "title": item.title,
                    "summary": item.summary,
                    "acceptance_criteria": list(item.acceptance_criteria),
                    "project_id": item.project_id,
                    "revision": item.revision,
                    "evidence_digest": item.evidence_digest,
                }
                for item in uow.list_work(project_id=project_id)
                if not allowed or item.state in allowed
            ]
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(rows, ensure_ascii=False))
    else:
        for row in rows:
            console.print(f"{row['id']}\t{row['state']}\t{row['title']}")


def _work_document(item: OperationalWorkRecord) -> dict[str, object]:
    return {
        "id": item.id,
        "project_id": item.project_id,
        "type": item.kind,
        "state": item.state,
        "title": item.title,
        "summary": item.summary,
        "acceptance_criteria": list(item.acceptance_criteria),
        "external_number": item.external_number,
        "revision": item.revision,
        "evidence_digest": item.evidence_digest,
    }


@app.command("show")
def show_command(
    work_item_id: Annotated[str, typer.Argument()],
    project: Annotated[str | None, typer.Option("--project", "--proje")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Tek bir Work kaydini kanonik SQLite authority'den okur."""
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            project_id = uow.resolve_project(project).id if project is not None else None
            document = _work_document(uow.resolve_work(work_item_id, project_id=project_id))
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"{document['id']}\t{document['state']}\t{document['title']}")


@app.command("history")
def history_command(
    work_item_id: Annotated[str, typer.Argument()],
    project: Annotated[str | None, typer.Option("--project", "--proje")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Work olaylarini append-only event kaydindan okur."""
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            project_id = uow.resolve_project(project).id if project is not None else None
            current = uow.resolve_work(work_item_id, project_id=project_id)
            events = uow.list_work_events(current.id)
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(list(events), ensure_ascii=False))
    else:
        for event in events:
            console.print(
                f"{event['revision']}\t{event['event_kind']}\t"
                f"{event['from_state'] or '-'}->{event['to_state']}"
            )


@app.command("transition")
def transition_command(
    work_item_id: Annotated[str, typer.Argument()],
    target: Annotated[WorkState, typer.Option("--hedef")],
    revision: Annotated[int, typer.Option("--revision")],
    project: Annotated[str | None, typer.Option("--project", "--proje")] = None,
    evidence_digest: Annotated[str | None, typer.Option("--evidence-digest")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Revision guard ve evidence ile Work durum gecisi yapar."""
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            project_id = uow.resolve_project(project).id if project is not None else None
            current = uow.resolve_work(work_item_id, project_id=project_id)
            work_item_id = current.id
            payload_digest = digest(
                {
                    "summary": current.summary,
                    "acceptance_criteria": list(current.acceptance_criteria),
                }
            )
            event_digest = digest(
                {
                    "work_item_id": work_item_id,
                    "revision": revision + 1,
                    "to_state": target.value,
                    "payload": payload_digest,
                }
            )
            preview: dict[str, Any] = {
                "work_item_id": work_item_id,
                "from_state": current.state,
                "to_state": target.value,
                "expected_revision": revision,
                "payload_digest": payload_digest,
                "event_digest": event_digest,
                "evidence_digest": evidence_digest,
                "apply": apply,
            }
            if apply:
                updated = uow.transition_work(
                    work_item_id=work_item_id,
                    expected_revision=revision,
                    to_state=target.value,
                    payload_digest=payload_digest,
                    event_digest=event_digest,
                    evidence_digest=evidence_digest,
                )
                preview["work"] = _work_document(updated)
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json or not apply:
        console.print_json(json.dumps(preview, ensure_ascii=False))
    else:
        console.print(f"Gecirildi: {work_item_id} ({target.value})")


def _json_object(raw: str, label: str) -> dict[str, Any]:
    if not raw or len(raw.encode("utf-8")) > _MAX_JSON_ARGUMENT_BYTES:
        raise ZekamError(f"{label} bounded JSON boyutunda olmali")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ZekamError(f"{label} JSON olmali") from exc
    if not isinstance(value, dict):
        raise ZekamError(f"{label} JSON nesnesi olmali")
    return value


def _run_document(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "work_item_id": run.work_item_id,
        "status": run.status,
        "plan_digest": run.plan_digest,
        "terminal_receipt_digest": run.terminal_receipt_digest,
    }


def _step_document(step: Any) -> dict[str, Any]:
    return {
        "id": step.id,
        "run_id": step.run_id,
        "step_key": step.step_key,
        "status": step.status,
        "input_digest": step.input_digest,
        "evidence_digest": step.evidence_digest,
    }


def _checkpoint_document(checkpoint: Any) -> dict[str, Any]:
    return {
        "id": checkpoint.id,
        "run_id": checkpoint.run_id,
        "sequence": checkpoint.sequence,
        "checkpoint_digest": checkpoint.checkpoint_digest,
    }


@app.command("run-create")
def run_create_command(
    work_item_id: Annotated[str, typer.Argument()],
    config_revision: Annotated[str, typer.Option("--config-revision")],
    plan_digest: Annotated[str, typer.Option("--plan-digest")],
    budget_json: Annotated[str, typer.Option("--budget-json")],
    source_snapshot: Annotated[str | None, typer.Option("--source-snapshot")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Work'e bagli planli bir run olusturur; provider cagirmadan SQLite'a yazar."""
    try:
        budget = _json_object(budget_json, "Run budget")
        if not apply:
            document = {
                "work_item_id": work_item_id,
                "config_revision_id": config_revision,
                "plan_digest": plan_digest,
                "budget_digest": digest(budget),
                "budget_keys": sorted(budget),
                "source_snapshot_id": source_snapshot,
                "apply": False,
            }
        else:
            store = sqlite_operational_store(home, realm)
            assert store is not None
            with store.unit_of_work() as uow:
                uow.get_work(work_item_id)
                run = uow.create_run(
                    work_item_id=work_item_id,
                    config_revision_id=config_revision,
                    plan_digest=plan_digest,
                    budget=budget,
                    source_snapshot_id=source_snapshot,
                )
                uow.commit()
            document = {"run": _run_document(run), "apply": True}
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json or not apply:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"Run olusturuldu: {document['run']['id']}")


@app.command("run-show")
def run_show_command(
    run_id: Annotated[str, typer.Argument()],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Run durumunu ve durable checkpoint ozetini salt okunur gosterir."""
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            run = uow.get_run(run_id)
            steps = [_step_document(item) for item in uow.list_run_steps(run_id)]
            checkpoints = [_checkpoint_document(item) for item in uow.list_checkpoints(run_id)]
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    document = {
        "run": _run_document(run),
        "steps": steps,
        "checkpoints": checkpoints,
        "checkpoint_count": len(checkpoints),
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"{run.id}\t{run.status}\tcheckpoint={len(checkpoints)}")


@app.command("run-transition")
def run_transition_command(
    run_id: Annotated[str, typer.Argument()],
    expected_status: Annotated[str, typer.Option("--beklenen-durum")],
    target: Annotated[str, typer.Option("--hedef")],
    terminal_receipt_digest: Annotated[
        str | None, typer.Option("--terminal-receipt-digest")
    ] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Run'i optimistic status ve terminal receipt ile ilerletir."""
    try:
        if not apply:
            document = {
                "run_id": run_id,
                "expected_status": expected_status,
                "target": target,
                "terminal_receipt_digest": terminal_receipt_digest,
                "apply": False,
            }
        else:
            store = sqlite_operational_store(home, realm)
            assert store is not None
            with store.unit_of_work() as uow:
                run = uow.transition_run(
                    run_id=run_id,
                    expected_status=expected_status,
                    to_status=target,
                    terminal_receipt_digest=terminal_receipt_digest,
                )
                uow.commit()
            document = {"run": _run_document(run), "apply": True}
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json or not apply:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"Run durumu guncellendi: {run_id} ({target})")


@app.command("run-step")
def run_step_command(
    run_id: Annotated[str, typer.Argument()],
    step_key: Annotated[str, typer.Argument()],
    input_digest: Annotated[str, typer.Option("--input-digest")],
    depends_on: Annotated[list[str] | None, typer.Option("--depends-on")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Run adimina bagimliliklariyla birlikte append-only adim ekler."""
    try:
        dependencies = tuple(depends_on or ())
        if not apply:
            document = {
                "run_id": run_id,
                "step_key": step_key,
                "input_digest": input_digest,
                "depends_on": list(dependencies),
                "apply": False,
            }
        else:
            store = sqlite_operational_store(home, realm)
            assert store is not None
            with store.unit_of_work() as uow:
                uow.get_run(run_id)
                step = uow.add_run_step(
                    run_id=run_id,
                    step_key=step_key,
                    input_digest=input_digest,
                    dependencies=dependencies,
                )
                uow.commit()
            document = {
                "step": _step_document(step),
                "depends_on": list(dependencies),
                "apply": True,
            }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json or not apply:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"Run adimi olusturuldu: {document['step']['id']}")


@app.command("run-step-transition")
def run_step_transition_command(
    step_id: Annotated[str, typer.Argument()],
    expected_status: Annotated[str, typer.Option("--beklenen-durum")],
    target: Annotated[str, typer.Option("--hedef")],
    evidence_digest: Annotated[str | None, typer.Option("--evidence-digest")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Run adimini dependency ve evidence kurallariyla ilerletir."""
    try:
        if not apply:
            document = {
                "step_id": step_id,
                "expected_status": expected_status,
                "target": target,
                "evidence_digest": evidence_digest,
                "apply": False,
            }
        else:
            store = sqlite_operational_store(home, realm)
            assert store is not None
            with store.unit_of_work() as uow:
                step = uow.transition_run_step(
                    step_id=step_id,
                    expected_status=expected_status,
                    to_status=target,
                    evidence_digest=evidence_digest,
                )
                uow.commit()
            document = {"step": _step_document(step), "apply": True}
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json or not apply:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"Run adimi guncellendi: {step_id} ({target})")


@app.command("run-steps")
def run_steps_command(
    run_id: Annotated[str, typer.Argument()],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Run adimlarini kanonik SQLite authority'den okur."""
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            rows = [_step_document(item) for item in uow.list_run_steps(run_id)]
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(rows, ensure_ascii=False))
    else:
        for row in rows:
            console.print(f"{row['id']}\t{row['status']}\t{row['step_key']}")


@app.command("checkpoint")
def checkpoint_command(
    run_id: Annotated[str, typer.Argument()],
    sequence: Annotated[int, typer.Option("--sequence")],
    payload_json: Annotated[str, typer.Option("--payload-json")],
    source_snapshot: Annotated[str | None, typer.Option("--source-snapshot")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Run icin digest'i payload'dan turetilen durable checkpoint yazar."""
    try:
        payload = _json_object(payload_json, "Checkpoint payload")
        checkpoint_digest = digest(payload)
        if not apply:
            document = {
                "run_id": run_id,
                "sequence": sequence,
                "checkpoint_digest": checkpoint_digest,
                "source_snapshot_id": source_snapshot,
                "apply": False,
            }
        else:
            store = sqlite_operational_store(home, realm)
            assert store is not None
            with store.unit_of_work() as uow:
                uow.get_run(run_id)
                checkpoint = uow.record_checkpoint(
                    run_id=run_id,
                    sequence=sequence,
                    checkpoint_digest=checkpoint_digest,
                    payload=payload,
                    source_snapshot_id=source_snapshot,
                )
                uow.commit()
            document = {"checkpoint": _checkpoint_document(checkpoint), "apply": True}
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json or not apply:
        console.print_json(json.dumps(document, ensure_ascii=False))
    else:
        console.print(f"Checkpoint yazildi: {document['checkpoint']['id']}")


@app.command("checkpoints")
def checkpoints_command(
    run_id: Annotated[str, typer.Argument()],
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Run checkpoint listesini kanonik SQLite authority'den okur."""
    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            uow.get_run(run_id)
            rows = [_checkpoint_document(item) for item in uow.list_checkpoints(run_id)]
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
    if output_json:
        console.print_json(json.dumps(rows, ensure_ascii=False))
    else:
        for row in rows:
            console.print(f"{row['sequence']}\t{row['checkpoint_digest']}")


@app.command("close")
def close_command(
    work_item_id: Annotated[str, typer.Argument()],
    revision: Annotated[int, typer.Option("--revision")],
    evidence_digest: Annotated[str, typer.Option("--evidence-digest")],
    project: Annotated[str | None, typer.Option("--project", "--proje")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Evidence ve revision kontrolu ile Work'u verification'dan kapatir."""
    transition_command(
        work_item_id=work_item_id,
        target=WorkState.COMPLETED,
        revision=revision,
        evidence_digest=evidence_digest,
        project=project,
        apply=apply,
        output_json=output_json,
        realm=realm,
        home=home,
    )
