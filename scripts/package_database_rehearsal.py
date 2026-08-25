"""Exercise shipped PostgreSQL migrations for package acceptance.

This harness is intentionally artifact-facing: CI installs the built wheel first,
then runs this script with isolated database credentials.  It emits only a
sanitized digest summary and never grants runtime authority.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import zekam
import zekam.infrastructure.postgres.migrations as migrations
from zekam.application.config import DatabaseSettings
from zekam.domain.canonical import digest
from zekam.infrastructure.postgres.connection import connect


def _settings() -> DatabaseSettings:
    return DatabaseSettings(
        host=os.environ.get("ZEKAM_DATABASE_HOST", "127.0.0.1"),
        port=int(os.environ.get("ZEKAM_DATABASE_PORT", "5432")),
        name=os.environ.get("ZEKAM_DATABASE_NAME", "zekam"),
        user=os.environ.get("ZEKAM_DATABASE_USER", "zekam"),
        sslmode=os.environ.get("ZEKAM_DATABASE_SSLMODE", "prefer"),
    )


def _installed_from_wheel() -> bool:
    package_path = Path(zekam.__file__).resolve()
    return "site-packages" in package_path.parts or "dist-packages" in package_path.parts


def _state_document(state: migrations.MigrationStatus) -> dict[str, Any]:
    return {
        "head": state.head,
        "applied_count": len(state.applied),
        "migration_digest": digest(
            [
                {
                    "version": item.version,
                    "name": item.name,
                    "checksum": item.checksum,
                }
                for item in state.applied
            ]
        ),
        "is_current": state.is_current,
    }


def upgrade_rollback_rehearsal() -> dict[str, Any]:
    with connect(_settings()) as connection:
        applied = migrations.upgrade(connection)
        upgraded = migrations.status(connection)
        if not upgraded.is_current or upgraded.head is None:
            raise RuntimeError("Shipped migrations did not reach current head")
        target = upgraded.head
        rolled_back = migrations.downgrade(connection, target=target)
        after_rollback = migrations.status(connection)
        if after_rollback.head != target - 1:
            raise RuntimeError("Exact head rollback did not move back one migration")
        reapplied = migrations.upgrade(connection, target=target)
        final = migrations.status(connection)
        if not final.is_current or final.head != target:
            raise RuntimeError("Reapplying the shipped head did not restore current state")
    return {
        "phase": "upgrade-rollback-reapply",
        "initial_apply_count": len(applied),
        "rolled_back_version": rolled_back.version,
        "reapply_count": len(reapplied),
        "state": _state_document(final),
    }


def verify_restored_database() -> dict[str, Any]:
    with connect(_settings()) as connection:
        restored = migrations.status(connection)
    if not restored.is_current:
        raise RuntimeError("Restored database is not at the shipped migration head")
    return {"phase": "backup-restore-verify", "state": _state_document(restored)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase", choices=("upgrade-rollback", "verify-restored"), help="Rehearsal phase"
    )
    parser.add_argument("--require-installed-wheel", action="store_true")
    args = parser.parse_args()
    if args.require_installed_wheel and not _installed_from_wheel():
        raise RuntimeError("Package rehearsal must import Zekam from an installed wheel")
    result = (
        upgrade_rollback_rehearsal()
        if args.phase == "upgrade-rollback"
        else verify_restored_database()
    )
    document = {
        "schema": "zekam-package-database-rehearsal/v1",
        "artifact_install": "wheel" if _installed_from_wheel() else "source",
        **result,
        "grants_authority": False,
    }
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
