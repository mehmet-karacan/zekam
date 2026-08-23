"""`zekam sandbox` ve `zekam git` komutlari.

Salt okunur dogrulama yuzeyleri: commit mesaji politikasi, push kapisi ve
sandbox politika ozeti. Hicbiri kendi basina mutation yapmaz.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from zekam.application.sandbox_delivery import default_policy
from zekam.domain.commit_policy import PushRequest, check_commit_message, evaluate_push
from zekam.domain.errors import ZekamError
from zekam.interfaces.cli.session import EXIT_POLICY_VIOLATION, fail_from

sandbox_app = typer.Typer(name="sandbox", help="Sandbox politika islemleri", no_args_is_help=True)
git_app = typer.Typer(name="git", help="Commit ve push kapisi", no_args_is_help=True)
console = Console()


@sandbox_app.command("policy")
def policy_command(
    path: Annotated[
        list[str], typer.Option("--yol", help="Yazilabilir relative path (tekrarlanabilir)")
    ],
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Verilen yollar icin sandbox politikasini ve digest'ini gosterir."""
    try:
        policy = default_policy(tuple(path))
    except ZekamError as exc:
        raise fail_from(exc) from exc
    payload = dict(policy.as_dict(), policy_digest=policy.policy_digest)
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title="Sandbox politikasi")
    table.add_column("Alan")
    table.add_column("Deger")
    table.add_row("yazilabilir yollar", ", ".join(sorted(policy.allowlist.entries)))
    table.add_row("network", "default-deny" if policy.network.is_default_deny else "kisitli izin")
    table.add_row("proje kaynagi", "exact-bound direct-write")
    table.add_row("proje kopyasi", "yasak")
    table.add_row("digest", policy.policy_digest)
    console.print(table)


@git_app.command("commit-check")
def commit_check_command(
    message_file: Annotated[
        Path | None, typer.Option("--dosya", help="Commit mesaji dosyasi")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Commit mesajini politikaya gore dogrular. Hook olarak kullanilabilir."""
    message = (
        message_file.read_text(encoding="utf-8") if message_file is not None else sys.stdin.read()
    )
    check = check_commit_message(message)
    if as_json:
        console.print_json(json.dumps(check.as_dict(), ensure_ascii=False))
    elif check.accepted:
        console.print("[green]Commit mesaji politikaya uygun.[/green]")
    else:
        for violation in check.violations:
            console.print(f"[red]{violation.code}:[/red] {violation.detail}")
    if not check.accepted:
        raise typer.Exit(EXIT_POLICY_VIOLATION)


@git_app.command("push-check")
def push_check_command(
    remote: Annotated[str, typer.Argument(help="Uzak depo adi")],
    branch: Annotated[str, typer.Argument(help="Dal adi")],
    head: Annotated[str, typer.Argument(help="Gonderilecek HEAD")],
    authorization: Annotated[
        str | None, typer.Option("--yetki-digest", help="Exact authorization digest")
    ] = None,
    user_requested: Annotated[
        bool, typer.Option("--kullanici-istedi", help="Kullanici acikca push istedi")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Force push denemesi")] = False,
    tests_passed: Annotated[bool, typer.Option("--test-gecti")] = False,
    verifier_passed: Annotated[bool, typer.Option("--verifier-gecti")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON cikti")] = False,
) -> None:
    """Push kapisini degerlendirir. Varsayilan karar reddir."""
    decision = evaluate_push(
        PushRequest(
            remote=remote,
            branch=branch,
            head=head,
            authorization_digest=authorization,
            user_requested=user_requested,
            force=force,
        ),
        tests_passed=tests_passed,
        verifier_passed=verifier_passed,
    )
    if as_json:
        console.print_json(json.dumps(decision.as_dict(), ensure_ascii=False))
    else:
        color = "green" if decision.allowed else "red"
        console.print(
            f"[{color}]{'izinli' if decision.allowed else 'reddedildi'}[/{color}]: "
            f"{decision.reason}"
        )
    if not decision.allowed:
        raise typer.Exit(EXIT_POLICY_VIOLATION)
