"""Migration-bound PostgreSQL function/procedure integrity.

The inventory is derived only from forward migrations whose exact checksum is
recorded in ``core.schema_migrations``.  Repair never accepts caller supplied
SQL: it can only replay the last canonical CREATE statement for a missing
routine, under an advisory lock and in one transaction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.infrastructure.postgres import migrations

ROUTINE_REPAIR_LOCK_KEY = 0x5A45_4B41_4D_52  # "ZEKAMR"

_CREATE_ROUTINE = re.compile(
    r"^\s*(?:(?:--[^\n]*(?:\n|$)|/\*.*?\*/)\s*)*"
    r"create\s+(?:or\s+replace\s+)?"
    r"(?P<kind>function|procedure)\s+"
    r"(?P<schema>[a-z_][a-z0-9_$]*)\.(?P<name>[a-z_][a-z0-9_$]*)\s*\(",
    re.IGNORECASE | re.DOTALL,
)
_DROP_ROUTINE = re.compile(
    r"^\s*(?:(?:--[^\n]*(?:\n|$)|/\*.*?\*/)\s*)*drop\s+"
    r"(?P<kind>function|procedure)\s+(?:if\s+exists\s+)?"
    r"(?P<schema>[a-z_][a-z0-9_$]*)\.(?P<name>[a-z_][a-z0-9_$]*)",
    re.IGNORECASE | re.DOTALL,
)
_POST_ROUTINE_STATEMENT = re.compile(
    r"^\s*(?:(?:--[^\n]*(?:\n|$)|/\*.*?\*/)\s*)*"
    r"(?:comment\s+on|grant\b|revoke\b|alter\s+)(?:.|\n)*$",
    re.IGNORECASE,
)


class RoutineKind(StrEnum):
    FUNCTION = "function"
    PROCEDURE = "procedure"

    @property
    def postgres_kind(self) -> str:
        return "f" if self is RoutineKind.FUNCTION else "p"


@dataclass(frozen=True, order=True, slots=True)
class RoutineKey:
    schema: str
    name: str
    kind: RoutineKind

    @property
    def label(self) -> str:
        return f"{self.schema}.{self.name}:{self.kind.value}"

    def as_dict(self) -> dict[str, str]:
        return {"schema": self.schema, "name": self.name, "kind": self.kind.value}


@dataclass(frozen=True, slots=True)
class RoutineSpec:
    key: RoutineKey
    migration_version: int
    migration_label: str
    migration_checksum: str
    statement: str
    post_statements: tuple[str, ...] = ()

    @property
    def statement_digest(self) -> str:
        return digest({"sql": self.statement})

    def as_dict(self) -> dict[str, Any]:
        return self.key.as_dict() | {
            "migration_version": self.migration_version,
            "migration_label": self.migration_label,
            "migration_checksum": self.migration_checksum,
            "statement_digest": self.statement_digest,
            "post_statement_digests": [
                digest({"sql": statement}) for statement in self.post_statements
            ],
        }


@dataclass(frozen=True, slots=True)
class RoutineIntegrityStatus:
    migration_head: int | None
    expected: tuple[RoutineSpec, ...]
    present: tuple[RoutineKey, ...]
    missing: tuple[RoutineSpec, ...]
    unexpected: tuple[RoutineKey, ...]
    migration_pending: tuple[str, ...]
    migration_drift: tuple[dict[str, Any], ...]

    @property
    def is_healthy(self) -> bool:
        return not self.missing and not self.migration_pending and not self.migration_drift

    @property
    def repair_plan_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-postgres-routine-repair-plan/v1",
                "migration_head": self.migration_head,
                "expected_count": len(self.expected),
                "missing": [item.as_dict() for item in self.missing],
                "migration_pending": list(self.migration_pending),
                "migration_drift": list(self.migration_drift),
            }
        )

    def as_dict(self) -> dict[str, Any]:
        matched_count = len(self.expected) - len(self.missing)
        return {
            "schema": "zekam-postgres-routine-integrity/v1",
            "migration_head": self.migration_head,
            "expected_count": len(self.expected),
            "present_count": matched_count,
            "observed_count": len(self.present),
            "missing_count": len(self.missing),
            "unexpected_count": len(self.unexpected),
            "missing": [item.as_dict() for item in self.missing],
            "unexpected": [item.as_dict() for item in self.unexpected],
            "migration_pending": list(self.migration_pending),
            "migration_drift": list(self.migration_drift),
            "repair_plan_digest": self.repair_plan_digest,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class RoutineRepairResult:
    repaired: tuple[RoutineKey, ...]
    plan_digest: str
    verified: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "repaired": [item.as_dict() for item in self.repaired],
            "repaired_count": len(self.repaired),
            "plan_digest": self.plan_digest,
            "verified": self.verified,
        }


def split_sql_statements(sql: str) -> tuple[str, ...]:
    """Split PostgreSQL SQL without breaking quoted or dollar-quoted bodies."""

    statements: list[str] = []
    start = 0
    index = 0
    quote: str | None = None
    dollar_tag: str | None = None
    line_comment = False
    block_depth = 0
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_depth:
            if char == "/" and following == "*":
                block_depth += 1
                index += 2
            elif char == "*" and following == "/":
                block_depth -= 1
                index += 2
            else:
                index += 1
            continue
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, index):
                index += len(dollar_tag)
                dollar_tag = None
            else:
                index += 1
            continue
        if quote is not None:
            if char == quote:
                if following == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "-" and following == "-":
            line_comment = True
            index += 2
            continue
        if char == "/" and following == "*":
            block_depth = 1
            index += 2
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "$":
            match = re.match(r"\$[a-zA-Z_][a-zA-Z0-9_]*\$|\$\$", sql[index:])
            if match is not None:
                dollar_tag = match.group(0)
                index += len(dollar_tag)
                continue
        if char == ";":
            statement = sql[start : index + 1].strip()
            if statement:
                statements.append(statement)
            start = index + 1
        index += 1
    trailing = sql[start:].strip()
    if trailing:
        statements.append(trailing)
    if quote is not None or dollar_tag is not None or block_depth:
        raise ValidationFailed("Migration SQL kapanmamis quote/comment iceriyor")
    return tuple(statements)


def expected_routines(
    connection: Any, directory: Path | None = None
) -> tuple[RoutineSpec, ...]:
    """Derive the final routine set from exact applied forward migrations."""

    available = migrations.discover_migrations(directory)
    applied = migrations.read_applied(connection)
    drift = migrations.detect_drift(applied, available)
    if drift:
        raise ConfigurationError("Migration drift varken routine inventory guvenilir degil")
    by_version = {item.version: item for item in available}
    state: dict[RoutineKey, RoutineSpec] = {}
    declarations: dict[RoutineKey, str] = {}
    applied_statements: list[str] = []
    for record in applied:
        migration = by_version.get(record.version)
        if migration is None or migration.checksum != record.checksum:
            raise ConfigurationError("Applied migration exact dosya/checksum ile eslesmiyor")
        for statement in split_sql_statements(migration.read_sql()):
            applied_statements.append(statement)
            create = _CREATE_ROUTINE.match(statement)
            if create is not None:
                key = _routine_key(create)
                declaration = _normalized_declaration(statement, create.end() - 1)
                prior = declarations.get(key)
                if prior is not None and prior != declaration:
                    raise ConfigurationError(
                        f"Overloaded routine otomatik repair kapsaminda degil: {key.label}"
                    )
                declarations[key] = declaration
                state[key] = RoutineSpec(
                    key=key,
                    migration_version=migration.version,
                    migration_label=migration.label,
                    migration_checksum=migration.checksum,
                    statement=statement,
                )
                continue
            drop = _DROP_ROUTINE.match(statement)
            if drop is not None:
                key = _routine_key(drop)
                state.pop(key, None)
                declarations.pop(key, None)
    for statement in applied_statements:
        if _POST_ROUTINE_STATEMENT.match(statement) is None:
            continue
        lowered = statement.lower()
        for key, spec in tuple(state.items()):
            if re.search(
                rf"(?<![a-z0-9_$]){re.escape(key.schema)}\."
                rf"{re.escape(key.name)}\s*\(",
                lowered,
            ):
                state[key] = replace(
                    spec, post_statements=(*spec.post_statements, statement)
                )
    return tuple(state[key] for key in sorted(state))


def present_routines(connection: Any, expected: tuple[RoutineSpec, ...]) -> tuple[RoutineKey, ...]:
    schemas = sorted({item.key.schema for item in expected})
    if not schemas:
        return ()
    with connection.cursor() as cursor:
        cursor.execute(
            "select namespace.nspname, procedure.proname, procedure.prokind "
            "from pg_proc procedure join pg_namespace namespace "
            "on namespace.oid = procedure.pronamespace "
            "where namespace.nspname = any(%s) and procedure.prokind in ('f','p') "
            "order by namespace.nspname, procedure.proname, procedure.prokind",
            (schemas,),
        )
        rows = cursor.fetchall()
    keys = {
        RoutineKey(
            schema=str(row[0]),
            name=str(row[1]),
            kind=RoutineKind.FUNCTION if str(row[2]) == "f" else RoutineKind.PROCEDURE,
        )
        for row in rows
    }
    return tuple(sorted(keys))


def status(connection: Any, directory: Path | None = None) -> RoutineIntegrityStatus:
    current = migrations.status(connection, directory)
    expected = expected_routines(connection, directory)
    present = present_routines(connection, expected)
    expected_by_key = {item.key: item for item in expected}
    present_set = set(present)
    return RoutineIntegrityStatus(
        migration_head=current.head,
        expected=expected,
        present=present,
        missing=tuple(expected_by_key[key] for key in sorted(expected_by_key.keys() - present_set)),
        unexpected=tuple(sorted(present_set - expected_by_key.keys())),
        migration_pending=tuple(item.label for item in current.pending),
        migration_drift=tuple(
            {"kind": item.kind.value, "version": item.version, "detail": item.detail}
            for item in current.drift
        ),
    )


def repair_missing_routines(
    connection: Any,
    *,
    plan_digest: str,
    directory: Path | None = None,
) -> RoutineRepairResult:
    """Replay only exact missing canonical CREATE statements and verify."""

    before = status(connection, directory)
    if before.repair_plan_digest != plan_digest:
        raise PolicyViolation("Routine repair plan digest stale veya exact degil")
    if before.migration_pending or before.migration_drift:
        raise PolicyViolation("Migration pending/drift varken routine repair reddedildi")
    if not before.missing:
        return RoutineRepairResult(repaired=(), plan_digest=plan_digest, verified=True)

    previous_autocommit = connection.autocommit
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("select pg_advisory_lock(%s)", (ROUTINE_REPAIR_LOCK_KEY,))
        try:
            connection.autocommit = False
            locked = status(connection, directory)
            if locked.repair_plan_digest != plan_digest:
                raise PolicyViolation("Routine repair plani lock alinirken stale oldu")
            with connection.cursor() as cursor:
                for routine in locked.missing:
                    cursor.execute(routine.statement)
                    for statement in routine.post_statements:
                        cursor.execute(statement)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("select pg_advisory_unlock(%s)", (ROUTINE_REPAIR_LOCK_KEY,))
    finally:
        connection.autocommit = previous_autocommit

    after = status(connection, directory)
    repaired_keys = tuple(item.key for item in before.missing)
    still_missing = {item.key for item in after.missing}.intersection(repaired_keys)
    if still_missing:
        raise ConfigurationError("Routine repair sonrasi exact dogrulama basarisiz")
    return RoutineRepairResult(repaired=repaired_keys, plan_digest=plan_digest, verified=True)


def _routine_key(match: re.Match[str]) -> RoutineKey:
    return RoutineKey(
        schema=match.group("schema").lower(),
        name=match.group("name").lower(),
        kind=RoutineKind(match.group("kind").lower()),
    )


def _normalized_declaration(statement: str, opening_parenthesis: int) -> str:
    """Return a conservative argument declaration fingerprint."""

    depth = 0
    index = opening_parenthesis
    while index < len(statement):
        char = statement[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return " ".join(statement[opening_parenthesis : index + 1].lower().split())
        index += 1
    raise ValidationFailed("Routine argument listesi kapanmamis")
