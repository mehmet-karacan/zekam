"""Authority-free lifecycle command-hook and local outbox CLI."""

from __future__ import annotations

import json
import sys
from typing import Annotated
from uuid import uuid4

import typer
from rich.console import Console

from zekam.application.client_lifecycle_spool import (
    MAX_PENDING_BATCH,
    ClientLifecycleSpool,
)
from zekam.application.home import resolve_home
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.identity import PRODUCT
from zekam.infrastructure.clients.codex_lifecycle import (
    CODEX_CLIENT_ID,
    CODEX_REVIEWED_VERSION,
    MAX_HOOK_INPUT_BYTES,
    assert_reviewed_codex_version,
    parse_codex_hook_input,
)
app = typer.Typer(name="client", help="Gercek istemci lifecycle hook ve yerel outbox yuzeyi")
console = Console()
error_console = Console(stderr=True)
HOME_HELP = f"{PRODUCT.data_root_env} kokunu gecici olarak ezer"
EXIT_RUNTIME_ERROR = 70
EXIT_POLICY_VIOLATION = 6


def _fail(message: str, *, code: int = EXIT_RUNTIME_ERROR) -> typer.Exit:
    error_console.print(f"[red]Hata:[/red] {message}")
    return typer.Exit(code)


def _fail_from(exc: ZekamError) -> typer.Exit:
    code = EXIT_POLICY_VIOLATION if isinstance(exc, PolicyViolation) else EXIT_RUNTIME_ERROR
    return _fail(str(exc), code=code)


def _codex_spool(home: str | None, client: str) -> ClientLifecycleSpool:
    if client != CODEX_CLIENT_ID:
        raise PolicyViolation("Yalniz reviewed Codex lifecycle contract destekleniyor")
    return ClientLifecycleSpool(resolve_home(home), client_id=client)


def _peek_event_name(payload: bytes) -> str | None:
    """Read only the allowlisted event discriminator for failure semantics."""

    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    value = document.get("hook_event_name")
    if value in {"PostCompact", "PreCompact", "SessionEnd", "SessionStart", "Stop"}:
        return str(value)
    return None


@app.command("hook")
def hook_command(
    client: Annotated[str, typer.Option("--client")] = CODEX_CLIENT_ID,
    client_version: Annotated[str, typer.Option("--client-version")] = CODEX_REVIEWED_VERSION,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Codex command-hook stdin'ini content-free immutable outbox'a yazar."""

    event_name: str | None = None
    try:
        spool = _codex_spool(home, client)
        assert_reviewed_codex_version(client_version)
        payload = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
        event_name = _peek_event_name(payload)
        envelope = parse_codex_hook_input(payload)
        spool.stage(
            envelope.observation_body(client_version=client_version),
            delivery_id=envelope.delivery_id(
                occurrence_id=str(uuid4()),
                client_version=client_version,
            ),
        )
    except (ZekamError, OSError) as exc:
        # Codex treats a command-hook failure as a lifecycle failure.  In
        # particular, Stop and PreCompact do not silently pass a missing local
        # durable observation.  Errors are sanitized and written only to stderr.
        if event_name == "PreCompact":
            error_console.print(
                "[red]Hata:[/red] Codex pre-compaction lifecycle observation durable degil"
            )
            console.print_json(json.dumps({"continue": False}))
            return
        raise _fail(
            "Codex lifecycle observation durable degil; yerel recovery gerekli",
            code=2,
        ) from exc

    # The hook response contains no model text, authority, path or transcript.
    # Empty JSON means the reviewed hook observed the event without overriding
    # Codex's own lifecycle decision.
    console.print_json("{}")


@app.command("status")
def status_command(
    client: Annotated[str, typer.Option("--client")] = CODEX_CLIENT_ID,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=MAX_PENDING_BATCH),
    ] = 100,
    after_sequence: Annotated[
        int,
        typer.Option("--after-sequence", min=0),
    ] = 0,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Yerel lifecycle outbox durumunun bounded queue-index sayfasini raporlar."""

    try:
        document = _codex_spool(home, client).status(
            limit=limit,
            after_sequence=after_sequence,
        )
    except (ZekamError, OSError) as exc:
        if isinstance(exc, OSError):
            raise _fail("Lifecycle spool okunamadi") from exc
        raise _fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False))


@app.command("pending")
def pending_command(
    client: Annotated[str, typer.Option("--client")] = CODEX_CLIENT_ID,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=MAX_PENDING_BATCH),
    ] = 100,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Governed worker icin bounded, dogrulanmis pending batch'i yazar."""

    try:
        entries = _codex_spool(home, client).pending(limit=limit)
    except (ZekamError, OSError) as exc:
        if isinstance(exc, OSError):
            raise _fail("Lifecycle spool okunamadi") from exc
        raise _fail_from(exc) from exc
    console.print_json(
        json.dumps(
            {
                "schema": "zekam-client-lifecycle-pending/v2",
                "client_id": client,
                "entries": [item.as_dict() for item in entries],
                "grants_authority": False,
            },
            ensure_ascii=False,
        )
    )


@app.command("drain")
def drain_command(
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=MAX_PENDING_BATCH),
    ] = 80,
    apply: Annotated[
        bool,
        typer.Option(
            "--uygula",
            help="Yalniz canonical worker ClaimedWork baglaminda uygulanabilir",
        ),
    ] = False,
    realm: Annotated[str, typer.Option("--realm")] = "yerel",
    client: Annotated[str, typer.Option("--client")] = CODEX_CLIENT_ID,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Bounded dry-run planini gosterir; public apply authority tasimaz."""

    try:
        if apply:
            # Owner token/lease/fence/envelope/effect claim are process-local
            # canonical worker state.  Reconstructing them from CLI arguments
            # would create a legacy direct-drain bypass.  Keep this gate before
            # spool construction, client-instance creation, or DB imports.
            raise PolicyViolation(
                "Lifecycle apply public CLI'dan yapilamaz; exact ClaimedWork worker gerekir"
            )
        spool = _codex_spool(home, client)
        pending = spool.pending(limit=limit)
        console.print_json(
            json.dumps(
                {
                    "schema": "zekam-client-lifecycle-drain-plan/v2",
                    "client_id": client,
                    "pending_count": len(pending),
                    "entry_digests": [item.entry_digest for item in pending],
                    "apply_surface": "canonical-worker-only",
                    "requires": [
                        "active-claimed-work",
                        "live-lease-fence-lock-envelope",
                        "preexisting-effect-claim",
                        "exact-one-shot-authorization",
                    ],
                    "applied": False,
                    "grants_authority": False,
                },
                ensure_ascii=False,
            )
        )
    except (ZekamError, OSError) as exc:
        if isinstance(exc, OSError):
            raise _fail("Lifecycle drain tamamlanamadi") from exc
        raise _fail_from(exc) from exc


def run() -> None:
    """Lightweight module entrypoint for the three-second SessionEnd budget."""

    app()


if __name__ == "__main__":  # pragma: no cover - real hook subprocess entrypoint
    run()
