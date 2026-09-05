"""Local operational work graph commands."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from zekam.domain.errors import ZekamError
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.work import WorkState, WorkType
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, fail_from, sqlite_operational_store

app = typer.Typer(name="work", help="Yerel work graph", no_args_is_help=True)
console = Console()
_MAX_ACTIVE_SPEC_BYTES = 1_048_576


@app.command("resume")
def resume_command(
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Read the canonical local work graph without granting authority."""

    try:
        store = sqlite_operational_store(home, realm)
        assert store is not None
        with store.unit_of_work() as uow:
            items = uow.list_work()
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
        for item in items
        if item.state not in {"completed", "cancelled"}
    ]
    document = {
        "source": "sqlite-work-graph",
        "open_items": open_items,
        "blocked": [item for item in open_items if item["state"] == "blocked"],
        "recent_activity": [],
        "next_safe_action": "ready work item sec" if open_items else "acik is bulunmuyor",
        "read_only": True,
        "grants_authority": False,
    }
    if output_json:
        console.print_json(json.dumps(document, ensure_ascii=False))
        return
    console.print(f"acik is: {len(open_items)}")


def _read_active_spec(path: Path, expected_digest: str) -> tuple[str, tuple[str, ...], str]:
    try:
        raw = path.read_bytes()
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
            )
            uow.commit()
    except ZekamError as exc:
        raise fail_from(exc) from exc
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
