"""WP-00 proof that legacy PostgreSQL access fails before any external effect."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import psycopg
import pytest

pytestmark = pytest.mark.security


def test_direct_postgresql_connect_is_blocked_before_network_access() -> None:
    with pytest.raises(RuntimeError, match="legacy-postgresql-access-forbidden: connect"):
        psycopg.connect("postgresql://127.0.0.1:1/forbidden")
    with pytest.raises(RuntimeError, match="legacy-postgresql-access-forbidden: connect"):
        psycopg.Connection.connect("postgresql://127.0.0.1:1/forbidden")
    with pytest.raises(RuntimeError, match="legacy-postgresql-access-forbidden: connect"):
        psycopg.AsyncConnection.connect(  # type: ignore[unused-coroutine]
            "postgresql://127.0.0.1:1/forbidden"
        )


@pytest.mark.parametrize(
    "command",
    [
        ["psql", "--version"],
        ["pg_dump", "--version"],
        ["pg_restore", "--version"],
        ["docker", "exec", "legacy-db", "psql", "--version"],
        ["docker", "exec", "legacy-db", "sh", "-c", "psql --version"],
        "pg_dumpall --version",
        Path("/usr/local/bin/psql"),
        [b"pg_dump", b"--version"],
    ],
)
def test_postgresql_clients_and_export_tools_are_blocked(
    command: list[str | bytes] | str | Path,
) -> None:
    with pytest.raises(RuntimeError, match="legacy-postgresql-access-forbidden: client="):
        subprocess.run(command, check=False)


def test_shell_escape_to_postgresql_client_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="legacy-postgresql-access-forbidden: client=psql"):
        os.system("psql --version")


def test_unrelated_local_process_remains_available() -> None:
    completed = subprocess.run(
        ["git", "--version"], check=True, capture_output=True, text=True
    )
    assert completed.returncode == 0
