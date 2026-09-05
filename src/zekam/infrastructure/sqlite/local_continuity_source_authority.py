# ruff: noqa: E501
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import stat
from contextlib import closing, suppress
from pathlib import Path
from uuid import UUID

from zekam.application.local_continuity import digest_text
from zekam.application.local_continuity_source_authority import (
    FileIdentity,
    LocalBindingRevision,
    PortableSourcePlanRecord,
    SourceAuthorityResult,
    _source_authority_now,
    _source_authority_timestamp,
    _SourceAuthorityReplay,
    authority_digest,
    strict_json,
)
from zekam.application.mutation_admission import (
    _advance_gate_a_source_capability,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_continuity_source_plan import (
    BoundedContinuitySource,
    _GuardedSQLite,
    _source_authority_baseline,
    _source_authority_cleanup,
    _source_authority_operational_unchanged,
    _source_authority_revision_values,
    _source_authority_uuid4,
    _SourceAuthorityDeadline,
    publish_portable_source_plan,
    read_portable_source_plan,
)
from zekam.infrastructure.local_continuity_source_plan import (
    _source_authority_connect as _connect,
)
from zekam.infrastructure.local_continuity_source_plan import (
    _source_authority_held_identity as _held_identity,
)
from zekam.infrastructure.local_continuity_source_plan import (
    _source_authority_identity as _identity,
)
from zekam.infrastructure.local_continuity_source_plan import (
    _source_authority_parent_chain as _parent_chain,
)
from zekam.infrastructure.sqlite.operational_schema import _validate_connection

SIDE_CAR_DDL = """CREATE TABLE local_source_authority_meta(
  singleton INTEGER PRIMARY KEY CHECK(typeof(singleton)='integer' AND singleton=1),
  schema_version INTEGER NOT NULL CHECK(typeof(schema_version)='integer' AND schema_version=1),
  schema_digest TEXT NOT NULL CHECK(typeof(schema_digest)='text' AND length(schema_digest)=71 AND substr(schema_digest,1,7)='sha256:' AND substr(schema_digest,8) NOT GLOB '*[^0-9a-f]*'),
  local_instance_id TEXT NOT NULL UNIQUE CHECK(typeof(local_instance_id)='text' AND length(local_instance_id)=36),
  created_at TEXT NOT NULL CHECK(typeof(created_at)='text' AND length(created_at)=27 AND substr(created_at,27,1)='Z')
) STRICT, WITHOUT ROWID;
CREATE TABLE local_source_authority_migration(
  version INTEGER PRIMARY KEY CHECK(typeof(version)='integer' AND version=1),
  name TEXT NOT NULL CHECK(typeof(name)='text' AND name='source-authority-v1'),
  checksum TEXT NOT NULL CHECK(typeof(checksum)='text' AND length(checksum)=71 AND substr(checksum,1,7)='sha256:' AND substr(checksum,8) NOT GLOB '*[^0-9a-f]*'),
  applied_at TEXT NOT NULL CHECK(typeof(applied_at)='text' AND length(applied_at)=27 AND substr(applied_at,27,1)='Z')
) STRICT, WITHOUT ROWID;
CREATE TABLE local_source_binding_revision(
  revision_digest TEXT PRIMARY KEY CHECK(typeof(revision_digest)='text' AND length(revision_digest)=71 AND substr(revision_digest,1,7)='sha256:' AND substr(revision_digest,8) NOT GLOB '*[^0-9a-f]*'),
  device_id TEXT NOT NULL CHECK(typeof(device_id)='text' AND length(CAST(device_id AS BLOB)) BETWEEN 1 AND 128),
  local_instance_id TEXT NOT NULL CHECK(typeof(local_instance_id)='text' AND length(local_instance_id)=36),
  operational_identity_digest TEXT NOT NULL CHECK(typeof(operational_identity_digest)='text' AND length(operational_identity_digest)=71 AND substr(operational_identity_digest,1,7)='sha256:' AND substr(operational_identity_digest,8) NOT GLOB '*[^0-9a-f]*'),
  operational_dev INTEGER NOT NULL CHECK(typeof(operational_dev)='integer' AND operational_dev>=0),
  operational_ino INTEGER NOT NULL CHECK(typeof(operational_ino)='integer' AND operational_ino>0),
  operational_uid INTEGER NOT NULL CHECK(typeof(operational_uid)='integer' AND operational_uid>=0),
  operational_gid INTEGER NOT NULL CHECK(typeof(operational_gid)='integer' AND operational_gid>=0),
  operational_mode INTEGER NOT NULL CHECK(typeof(operational_mode)='integer' AND operational_mode>=0),
  operational_nlink INTEGER NOT NULL CHECK(typeof(operational_nlink)='integer' AND operational_nlink=1),
  operational_birthtime_ns INTEGER NOT NULL CHECK(typeof(operational_birthtime_ns)='integer' AND operational_birthtime_ns>=0),
  project_id TEXT NOT NULL CHECK(typeof(project_id)='text' AND length(project_id)=36),
  source_binding_id TEXT NOT NULL CHECK(typeof(source_binding_id)='text' AND length(source_binding_id)=36),
  root_path TEXT NOT NULL CHECK(typeof(root_path)='text' AND length(CAST(root_path AS BLOB)) BETWEEN 1 AND 4096),
  root_path_digest TEXT NOT NULL CHECK(typeof(root_path_digest)='text' AND length(root_path_digest)=71 AND substr(root_path_digest,1,7)='sha256:' AND substr(root_path_digest,8) NOT GLOB '*[^0-9a-f]*'),
  root_dev INTEGER NOT NULL CHECK(typeof(root_dev)='integer' AND root_dev>=0),
  root_ino INTEGER NOT NULL CHECK(typeof(root_ino)='integer' AND root_ino>0),
  root_uid INTEGER NOT NULL CHECK(typeof(root_uid)='integer' AND root_uid>=0),
  root_gid INTEGER NOT NULL CHECK(typeof(root_gid)='integer' AND root_gid>=0),
  root_mode INTEGER NOT NULL CHECK(typeof(root_mode)='integer' AND root_mode>=0),
  root_nlink INTEGER NOT NULL CHECK(typeof(root_nlink)='integer' AND root_nlink>=1),
  root_birthtime_ns INTEGER NOT NULL CHECK(typeof(root_birthtime_ns)='integer' AND root_birthtime_ns>=0),
  portable_plan_digest TEXT NOT NULL CHECK(typeof(portable_plan_digest)='text' AND length(portable_plan_digest)=71 AND substr(portable_plan_digest,1,7)='sha256:' AND substr(portable_plan_digest,8) NOT GLOB '*[^0-9a-f]*'),
  previous_revision_digest TEXT CHECK(previous_revision_digest IS NULL OR (typeof(previous_revision_digest)='text' AND length(previous_revision_digest)=71 AND substr(previous_revision_digest,1,7)='sha256:' AND substr(previous_revision_digest,8) NOT GLOB '*[^0-9a-f]*')),
  generation INTEGER NOT NULL CHECK(typeof(generation)='integer' AND generation BETWEEN 1 AND 64),
  body_json TEXT NOT NULL CHECK(typeof(body_json)='text' AND length(CAST(body_json AS BLOB)) BETWEEN 1 AND 8192 AND json_valid(body_json)=1),
  created_at TEXT NOT NULL CHECK(typeof(created_at)='text' AND length(created_at)=27 AND substr(created_at,27,1)='Z'),
  grants_authority INTEGER NOT NULL CHECK(typeof(grants_authority)='integer' AND grants_authority=0),
  approval_inherited INTEGER NOT NULL CHECK(typeof(approval_inherited)='integer' AND approval_inherited=0),
  CHECK((generation=1 AND previous_revision_digest IS NULL) OR (generation>1 AND previous_revision_digest IS NOT NULL)),
  FOREIGN KEY(local_instance_id) REFERENCES local_source_authority_meta(local_instance_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  FOREIGN KEY(previous_revision_digest) REFERENCES local_source_binding_revision(revision_digest) ON UPDATE RESTRICT ON DELETE RESTRICT
) STRICT, WITHOUT ROWID;
CREATE UNIQUE INDEX local_source_binding_revision_scope_uq ON local_source_binding_revision(device_id,source_binding_id,generation);
CREATE UNIQUE INDEX local_source_binding_revision_link_uq ON local_source_binding_revision(device_id,source_binding_id,generation,revision_digest);
CREATE TABLE local_source_binding_head(
  device_id TEXT NOT NULL CHECK(typeof(device_id)='text' AND length(CAST(device_id AS BLOB)) BETWEEN 1 AND 128),
  source_binding_id TEXT NOT NULL CHECK(typeof(source_binding_id)='text' AND length(source_binding_id)=36),
  generation INTEGER NOT NULL CHECK(typeof(generation)='integer' AND generation BETWEEN 1 AND 64),
  revision_digest TEXT NOT NULL CHECK(typeof(revision_digest)='text' AND length(revision_digest)=71 AND substr(revision_digest,1,7)='sha256:' AND substr(revision_digest,8) NOT GLOB '*[^0-9a-f]*'),
  previous_generation INTEGER CHECK(previous_generation IS NULL OR (typeof(previous_generation)='integer' AND previous_generation BETWEEN 1 AND 63)),
  previous_revision_digest TEXT CHECK(previous_revision_digest IS NULL OR (typeof(previous_revision_digest)='text' AND length(previous_revision_digest)=71 AND substr(previous_revision_digest,1,7)='sha256:' AND substr(previous_revision_digest,8) NOT GLOB '*[^0-9a-f]*')),
  created_at TEXT NOT NULL CHECK(typeof(created_at)='text' AND length(created_at)=27 AND substr(created_at,27,1)='Z'),
  PRIMARY KEY(device_id,source_binding_id,generation),
  UNIQUE(revision_digest),
  CHECK((generation=1 AND previous_generation IS NULL AND previous_revision_digest IS NULL) OR (generation>1 AND previous_generation=generation-1 AND previous_revision_digest IS NOT NULL)),
  FOREIGN KEY(device_id,source_binding_id,generation,revision_digest) REFERENCES local_source_binding_revision(device_id,source_binding_id,generation,revision_digest) ON UPDATE RESTRICT ON DELETE RESTRICT,
  FOREIGN KEY(device_id,source_binding_id,previous_generation,previous_revision_digest) REFERENCES local_source_binding_head(device_id,source_binding_id,generation,revision_digest) ON UPDATE RESTRICT ON DELETE RESTRICT
) STRICT, WITHOUT ROWID;
CREATE INDEX local_source_binding_head_latest_ix ON local_source_binding_head(device_id,source_binding_id,generation DESC);
CREATE UNIQUE INDEX local_source_binding_head_link_uq ON local_source_binding_head(device_id,source_binding_id,generation,revision_digest);
CREATE TRIGGER local_source_authority_meta_no_update BEFORE UPDATE ON local_source_authority_meta BEGIN SELECT RAISE(ABORT,'immutable meta'); END;
CREATE TRIGGER local_source_authority_meta_no_delete BEFORE DELETE ON local_source_authority_meta BEGIN SELECT RAISE(ABORT,'immutable meta'); END;
CREATE TRIGGER local_source_authority_migration_no_update BEFORE UPDATE ON local_source_authority_migration BEGIN SELECT RAISE(ABORT,'immutable migration'); END;
CREATE TRIGGER local_source_authority_migration_no_delete BEFORE DELETE ON local_source_authority_migration BEGIN SELECT RAISE(ABORT,'immutable migration'); END;
CREATE TRIGGER local_source_binding_revision_no_update BEFORE UPDATE ON local_source_binding_revision BEGIN SELECT RAISE(ABORT,'immutable revision'); END;
CREATE TRIGGER local_source_binding_revision_no_delete BEFORE DELETE ON local_source_binding_revision BEGIN SELECT RAISE(ABORT,'immutable revision'); END;
CREATE TRIGGER local_source_binding_head_no_update BEFORE UPDATE ON local_source_binding_head BEGIN SELECT RAISE(ABORT,'immutable head'); END;
CREATE TRIGGER local_source_binding_head_no_delete BEFORE DELETE ON local_source_binding_head BEGIN SELECT RAISE(ABORT,'immutable head'); END;
"""

DDL_DIGEST = "sha256:de20bb4d32db5f54e19069ceb1a50961924455cde16d689a866a9cb9058eb945"
SCHEMA_FINGERPRINT = "sha256:6e13eb1daef35e22ac8c7c552b533176e87d56f834a8e18aae28927c5f8779c3"
OPERATIONAL_SCHEMA_DIGEST = (
    "sha256:e3dd4973ffd2af800d40e513d0ec42a4f87f12ce1b49648053833e631f6bf2e0"
)
if "sha256:" + hashlib.sha256(SIDE_CAR_DDL.encode()).hexdigest() != DDL_DIGEST:
    raise RuntimeError("Local source authority DDL drift")
if (
    authority_digest(
        "zekam.local-source-authority.sqlite-schema.v1",
        {"ddl_sha256": DDL_DIGEST, "schema": "zekam-local-source-authority-schema-fingerprint/v1"},
    )
    != SCHEMA_FINGERPRINT
):
    raise RuntimeError("Local source authority schema fingerprint drift")


def _schema_rows(db: sqlite3.Connection | _GuardedSQLite) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in db.execute(
            "select type,name,tbl_name,sql from sqlite_schema where name not like 'sqlite_stat%' order by type,name"
        )
    )


def _expected_schema_rows() -> tuple[tuple[object, ...], ...]:
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(SIDE_CAR_DDL)
        return _schema_rows(db)


_EXPECTED_SCHEMA_ROWS = _expected_schema_rows()


def _validated_candidate(row: sqlite3.Row) -> LocalBindingRevision:
    try:
        raw = row["body_blob"]
        if not isinstance(raw, bytes) or not 1 <= len(raw) <= 8192:
            raise ValueError
        body = strict_json(raw, maximum=8192)
        operational = body["operational_identity"]
        root = body["root"]
        if type(operational) is not dict or type(root) is not dict:
            raise ValueError
        candidate = LocalBindingRevision(
            row["device_id"],
            row["local_instance_id"],
            FileIdentity(
                row["operational_dev"],
                row["operational_ino"],
                row["operational_uid"],
                row["operational_gid"],
                row["operational_mode"],
                row["operational_nlink"],
                row["operational_birthtime_ns"],
            ),
            operational["parent_chain_digest"],
            row["project_id"],
            row["source_binding_id"],
            row["root_path"],
            FileIdentity(
                row["root_dev"],
                row["root_ino"],
                row["root_uid"],
                row["root_gid"],
                row["root_mode"],
                row["root_nlink"],
                row["root_birthtime_ns"],
            ),
            row["portable_plan_digest"],
            row["previous_revision_digest"],
            row["generation"],
            row["created_at"],
        )
    except (KeyError, TypeError, ValueError, ValidationFailed):
        raise PolicyViolation("Local source authority revision body drift") from None
    if candidate.body() != body or tuple(row)[:29] != _source_authority_revision_values(candidate):
        raise PolicyViolation("Local source authority revision body drift")
    return candidate


def _validate(db: sqlite3.Connection | _GuardedSQLite, *, physical: bool = True) -> str:
    page_size = db.execute("pragma page_size").fetchone()[0]
    page_count = db.execute("pragma page_count").fetchone()[0]
    if (
        db.execute("pragma foreign_keys").fetchone()[0] != 1
        or page_size != 4096
        or db.execute("pragma journal_mode").fetchone()[0].lower() != "delete"
        or db.execute("pragma locking_mode").fetchone()[0].lower() != "normal"
        or db.execute("pragma synchronous").fetchone()[0] != 2
        or _schema_rows(db) != _EXPECTED_SCHEMA_ROWS
    ):
        raise PolicyViolation("Local source authority schema drift")
    meta = db.execute("select * from local_source_authority_meta").fetchall()
    ledger = db.execute("select * from local_source_authority_migration").fetchall()
    if (
        len(meta) != 1
        or meta[0]["singleton"] != 1
        or meta[0]["schema_version"] != 1
        or meta[0]["schema_digest"] != SCHEMA_FINGERPRINT
        or len(ledger) != 1
        or tuple(ledger[0]) != (1, "source-authority-v1", DDL_DIGEST, meta[0]["created_at"])
        or db.execute("pragma foreign_key_check").fetchone() is not None
        or page_count > 2048
        or db.execute("select count(*) from local_source_binding_revision").fetchone()[0] > 4096
    ):
        raise PolicyViolation("Local source authority metadata drift")
    if physical:
        databases = db.execute("pragma database_list").fetchall()
        if len(databases) != 1 or databases[0][1] != "main" or not databases[0][2]:
            raise PolicyViolation("Local source authority database identity drift")
        database_path = Path(str(databases[0][2]))
        _identity(database_path, regular=True)
        if database_path.stat(follow_symlinks=False).st_size != page_size * page_count:
            raise PolicyViolation("Local source authority physical size drift")
    local_instance_id = str(meta[0]["local_instance_id"])
    try:
        parsed = UUID(local_instance_id)
    except (TypeError, ValueError, AttributeError):
        raise PolicyViolation("Local source authority metadata drift") from None
    if parsed.version != 4 or str(parsed) != local_instance_id:
        raise PolicyViolation("Local source authority metadata drift")
    _source_authority_timestamp(meta[0]["created_at"])
    rows = db.execute(
        "select r.*,cast(r.body_json as blob) as body_blob,h.previous_generation,"
        "h.previous_revision_digest as head_previous from local_source_binding_revision r "
        "join local_source_binding_head h on h.device_id=r.device_id "
        "and h.source_binding_id=r.source_binding_id and h.generation=r.generation "
        "and h.revision_digest=r.revision_digest order by r.device_id,r.source_binding_id,r.generation"
    ).fetchall()
    if len(rows) != db.execute("select count(*) from local_source_binding_revision").fetchone()[0]:
        raise PolicyViolation("Local source authority revision/head cardinality drift")
    for row in rows:
        candidate = _validated_candidate(row)
        if (row["previous_generation"], row["head_previous"]) != (
            None if candidate.generation == 1 else candidate.generation - 1,
            candidate.previous_revision_digest,
        ):
            raise PolicyViolation("Local source authority head body drift")
    return local_instance_id


class SQLiteLocalSourceAuthority:
    def __init__(self, home: Path, operational_path: Path) -> None:
        if (
            not isinstance(home, Path)
            or not home.is_absolute()
            or not isinstance(operational_path, Path)
            or not operational_path.is_absolute()
        ):
            raise ValidationFailed("Local source authority typed absolute paths required")
        self.home = home
        self.path = home / "yerel" / "source-authority.sqlite3"
        self.operational_path = operational_path
        if operational_path != home / "state" / "operational.db":
            raise ValidationFailed("Local source authority fixed operational path required")

    def _preflight(self, *, create: bool) -> None:
        _identity(self.home, regular=False)
        _identity(self.home / "yerel", regular=False)
        _identity(self.operational_path, regular=True)
        try:
            identity = _identity(self.path, regular=True)
            if stat.S_IMODE(identity.mode) != 0o600:
                raise PolicyViolation("Local source authority file mode rejected")
        except FileNotFoundError:
            if not create:
                raise PolicyViolation("Local source authority is not initialized") from None
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                Path(str(self.path) + suffix).lstat()
            except FileNotFoundError:
                continue
            else:
                raise PolicyViolation("Local source authority side file rejected")

    def _bootstrap(self, timestamp: str) -> None:
        self._preflight(create=True)
        try:
            _identity(self.path, regular=True)
            return
        except FileNotFoundError:
            pass
        stale = sorted(self.path.parent.glob(".source-authority.sqlite3.bootstrap-*"))
        if len(stale) > 2:
            raise PolicyViolation("Local source authority bootstrap census rejected")
        for item in stale:
            info = item.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size
            ):
                raise PolicyViolation("Local source authority bootstrap residue rejected")
            item.unlink()
        local_instance_id = _source_authority_uuid4()
        memory = sqlite3.connect(":memory:")
        try:
            memory.row_factory = sqlite3.Row
            memory.execute("pragma page_size=4096")
            memory.execute("pragma foreign_keys=on")
            memory.executescript(SIDE_CAR_DDL)
            memory.execute(
                "insert into local_source_authority_meta values(1,1,?,?,?)",
                (SCHEMA_FINGERPRINT, local_instance_id, timestamp),
            )
            memory.execute(
                "insert into local_source_authority_migration values(1,'source-authority-v1',?,?)",
                (DDL_DIGEST, timestamp),
            )
            memory.commit()
            raw = memory.serialize()
        finally:
            memory.close()
        temporary = (
            self.path.parent / f".source-authority.sqlite3.bootstrap-{secrets.token_hex(16)}"
        )
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            try:
                written = 0
                while written < len(raw):
                    count = os.write(descriptor, raw[written:])
                    if count <= 0:
                        raise OSError("short bootstrap write")
                    written += count
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            with suppress(FileExistsError):
                os.link(temporary, self.path, follow_symlinks=False)
            temporary.unlink(missing_ok=True)
            self._sync()
            with closing(_connect(self.path, readonly=True)) as db:
                _validate(db)
        except BaseException as exc:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            if isinstance(exc, (OSError, sqlite3.Error, PolicyViolation)):
                raise PolicyViolation("Local source authority bootstrap failed") from exc
            raise

    def _sync(self) -> None:
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        parent = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
            os.fsync(parent)
        finally:
            os.close(descriptor)
            os.close(parent)

    def _operational_identity(self) -> tuple[FileIdentity, str]:
        return _identity(self.operational_path, regular=True), _parent_chain(
            self.operational_path, self.home
        )

    def _operational_snapshot(
        self, record: PortableSourcePlanRecord, *, fenced: bool
    ) -> tuple[int, _GuardedSQLite, tuple[object, ...]]:
        descriptor = os.open(self.operational_path, os.O_RDWR | os.O_NOFOLLOW)
        db: sqlite3.Connection | None = None
        try:
            identity = _identity(self.operational_path, regular=True)
            if _held_identity(descriptor) != identity:
                raise PolicyViolation("Operational identity changed")
            db = sqlite3.connect(f"file:/dev/fd/{descriptor}?mode=rw", uri=True, timeout=0.125)
            db.row_factory = sqlite3.Row
            db.execute("pragma foreign_keys=on")
            db.execute("pragma busy_timeout=125")
            if _validate_connection(db) != 4:
                raise PolicyViolation("Source authority requires dormant V4")
            guarded = _GuardedSQLite(db)
            if fenced:
                guarded.execute("OP_BEGIN", "begin immediate")
            recipe = record.plan.recipe
            rows = guarded.execute(
                "OP_SOURCE",
                "select p.id,b.id,r.realm_id,s.id,s.revision_ref,s.tree_digest,s.content_digest,"
                "s.config_digest,c.task_digest,c.config_digest from project p join source_binding b "
                "on b.project_id=p.id join source_snapshot s on s.source_binding_id=b.id join "
                "project_knowledge_realm r on r.project_id=p.id join config_revision c on c.active=1 "
                "where p.id=? and b.id=? and s.id=? and p.status='active' and b.active=1 "
                "and b.source_kind='git'",
                (recipe.project_id, recipe.source_binding_id, record.source_snapshot_id),
            ).fetchall()
            latest = guarded.execute(
                "OP_LATEST",
                "select id from source_snapshot where source_binding_id=? order by captured_at desc,id desc limit 1",
                (recipe.source_binding_id,),
            ).fetchone()
            baseline = (
                identity,
                tuple(tuple(row) for row in rows),
                None if latest is None else latest[0],
            )
            expected = (
                recipe.project_id,
                recipe.source_binding_id,
                recipe.realm_id,
                record.source_snapshot_id,
                record.plan.revision_ref,
                record.plan.tree_digest,
                record.plan.content_digest,
                record.plan.config_digest,
                recipe.task_digest,
                recipe.policy_digest,
            )
            if baseline[1] != (expected,) or baseline[2] != record.source_snapshot_id:
                raise PolicyViolation("Operational source snapshot mismatch")
            return descriptor, guarded, baseline
        except BaseException:
            if db is not None:
                db.close()
            os.close(descriptor)
            raise

    def execute(
        self,
        *,
        capability: object,
        record: PortableSourcePlanRecord,
        source: object,
        device_id: str,
        root: Path,
        previous_revision_digest: str | None,
        rebind: bool,
    ) -> SourceAuthorityResult:
        deadline = _SourceAuthorityDeadline()
        if type(record) is not PortableSourcePlanRecord or type(rebind) is not bool:
            raise ValidationFailed("Local source authority typed bind request required")
        if type(source) is not BoundedContinuitySource:
            raise ValidationFailed("Local source authority concrete source required")
        command = _advance_gate_a_source_capability(capability, "INPUTS_VALID", "FIRST_CAPTURED")
        if command != ("continuity", "source-rebind" if rebind else "source-bind"):
            raise PolicyViolation("Local source authority command mismatch")
        record.__post_init__()
        if type(device_id) is not str or not 1 <= len(device_id.encode("utf-8")) <= 128:
            raise ValidationFailed("Local source authority bounded device required")
        if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
            raise ValidationFailed("Local source authority absolute source root required")
        if previous_revision_digest is not None:
            digest_text(previous_revision_digest)
        if rebind != (previous_revision_digest is not None):
            raise ValidationFailed("Local source authority predecessor mode mismatch")
        deadline.check()
        first = source.capture()
        deadline.check()
        if first != record.plan:
            raise PolicyViolation("Local source authority first capture mismatch")
        tentative_fd, tentative, baseline0 = self._operational_snapshot(record, fenced=False)
        try:
            tentative.close()
        finally:
            os.close(tentative_fd)
        deadline.check()
        self._preflight(create=not rebind)
        if not rebind and not self.path.exists():
            self._bootstrap(_source_authority_now())
        deadline.check()
        operational, parent_digest = self._operational_identity()
        root_identity = _identity(root, regular=False)
        commit_started = False
        candidate: LocalBindingRevision | None = None
        replay_detected = False
        operational_db: _GuardedSQLite | None = None
        operational_fd: int | None = None
        root_fd: int | None = None
        side_fd: int | None = None
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            side_fd = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW)
            side_identity = _identity(self.path, regular=True)
            if _held_identity(side_fd) != side_identity or _held_identity(root_fd) != root_identity:
                raise PolicyViolation("Local source authority file changed")
            with closing(_connect(self.path, readonly=False)) as raw_db:
                raw_db.execute("pragma journal_mode=delete")
                raw_db.execute("pragma synchronous=full")
                local_instance_id = _validate(raw_db)
                baseline = raw_db.serialize()
                baseline_rows = _source_authority_baseline(raw_db)
                db = _GuardedSQLite(raw_db)
                db.execute("SIDE_BEGIN", "begin exclusive")
                if (
                    _source_authority_baseline(db, "LOCKED") != baseline_rows
                    or _identity(self.path, regular=True) != side_identity
                    or _held_identity(side_fd) != side_identity
                ):
                    raise PolicyViolation("Local source authority B0 drift")
                operational_fd, operational_db, baseline1 = self._operational_snapshot(
                    record, fenced=True
                )
                if baseline1 != baseline0:
                    raise PolicyViolation("Operational source snapshot changed")
                deadline.check()
                if source.capture() != first:
                    raise PolicyViolation("Local source authority second capture mismatch")
                deadline.check()
                rows = db.execute(
                    "SIDE_HEADS",
                    "select generation,revision_digest from local_source_binding_head where device_id=? and source_binding_id=? order by generation desc limit 1",
                    (device_id, record.plan.recipe.source_binding_id),
                ).fetchall()
                replay_row = db.execute(
                    "SIDE_REPLAY",
                    "select r.*,cast(r.body_json as blob) as body_blob,h.previous_generation,"
                    "h.previous_revision_digest as head_previous from local_source_binding_revision r "
                    "join local_source_binding_head h on h.device_id=r.device_id and "
                    "h.source_binding_id=r.source_binding_id and h.generation=r.generation and "
                    "h.revision_digest=r.revision_digest where r.device_id=? and r.source_binding_id=? "
                    "order by r.generation desc limit 1",
                    (device_id, record.plan.recipe.source_binding_id),
                ).fetchone()
                if replay_row is not None:
                    replayed = _validated_candidate(replay_row)
                    expected_previous = previous_revision_digest
                    if (
                        replayed.previous_revision_digest == expected_previous
                        and replayed.portable_plan_digest == record.plan.content_digest
                        and replayed.operational_identity == operational
                        and replayed.root_identity == root_identity
                        and replayed.root_path == str(root)
                    ):
                        candidate = replayed
                        replay_detected = True
                        raise _SourceAuthorityReplay
                if rebind:
                    if len(rows) != 1 or rows[0]["revision_digest"] != previous_revision_digest:
                        raise PolicyViolation("Local source rebind predecessor mismatch")
                    generation = int(rows[0]["generation"]) + 1
                    previous = str(rows[0]["revision_digest"])
                else:
                    if rows or previous_revision_digest is not None:
                        raise PolicyViolation("Local source binding already exists")
                    generation, previous = 1, None
                timestamp = _source_authority_now()
                candidate = LocalBindingRevision(
                    device_id,
                    local_instance_id,
                    operational,
                    parent_digest,
                    record.plan.recipe.project_id,
                    record.plan.recipe.source_binding_id,
                    str(root),
                    root_identity,
                    record.plan.content_digest,
                    previous,
                    generation,
                    timestamp,
                )
                if db.execute("SIDE_PAGE_PRE", "pragma page_count").fetchone()[0] > 2048:
                    raise PolicyViolation("Local source authority capacity exceeded")
                head_bytes = sum(
                    len(value.encode("utf-8"))
                    for value in (
                        candidate.device_id,
                        candidate.source_binding_id,
                        candidate.revision_digest,
                        candidate.previous_revision_digest or "",
                        candidate.created_at,
                    )
                )
                if (
                    db.execute("SIDE_PAGE_PROJECTED", "pragma page_count").fetchone()[0] * 4096
                    + 65536
                    + 4 * (len(candidate.body_json.encode("utf-8")) + head_bytes)
                    > 8 * 1024 * 1024
                ):
                    raise PolicyViolation("Local source authority projected capacity exceeded")
                latest = db.execute(
                    "SIDE_LATEST",
                    "select generation,revision_digest from local_source_binding_head where device_id=? and source_binding_id=? order by generation desc limit 1",
                    (device_id, record.plan.recipe.source_binding_id),
                ).fetchone()
                if (latest is None) != (generation == 1) or (
                    latest is not None and tuple(latest) != (generation - 1, previous)
                ):
                    raise PolicyViolation("Local source authority concurrent head drift")
                clone = sqlite3.connect(":memory:")
                try:
                    clone.deserialize(baseline)
                    clone.execute("pragma foreign_keys=on")
                    clone.execute("begin")
                    clone.execute(
                        "insert into local_source_binding_revision values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        _source_authority_revision_values(candidate),
                    )
                    clone.rollback()
                finally:
                    clone.close()
                publish_portable_source_plan(self.home, record)
                deadline.check()
                if source.capture() != first:
                    raise PolicyViolation("Local source authority precommit capture mismatch")
                if (
                    read_portable_source_plan(
                        self.home, record.plan.recipe.project_id, record.plan.content_digest
                    )
                    != record
                    or operational_fd is None
                    or not _source_authority_operational_unchanged(
                        self.operational_path, self.home, operational_fd, operational, parent_digest
                    )
                ):
                    raise PolicyViolation("Local source authority precommit evidence drift")
                deadline.check()
                db.execute(
                    "SIDE_REVISION_INSERT",
                    "insert into local_source_binding_revision values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    _source_authority_revision_values(candidate),
                )
                db.execute(
                    "SIDE_HEAD_INSERT",
                    "insert into local_source_binding_head values(?,?,?,?,?,?,?)",
                    (
                        candidate.device_id,
                        candidate.source_binding_id,
                        candidate.generation,
                        candidate.revision_digest,
                        None if candidate.generation == 1 else candidate.generation - 1,
                        candidate.previous_revision_digest,
                        candidate.created_at,
                    ),
                )
                if db.execute("SIDE_PAGE_POST", "pragma page_count").fetchone()[0] > 2048:
                    raise PolicyViolation("Local source authority capacity exceeded")
                _validate(db, physical=False)
                commit_started = True
                db.commit()
                deadline.check()
                if (
                    _identity(self.path, regular=True) != side_identity
                    or _held_identity(side_fd) != side_identity
                ):
                    raise PolicyViolation("Local source authority commit identity drift")
            if candidate is None or replay_detected:
                raise PolicyViolation("Local source authority write plan unavailable")
            self._sync()
            deadline.check()
            verified = self._classify(candidate)
            deadline.check()
            if (
                verified is None
                or operational_fd is None
                or not _source_authority_operational_unchanged(
                    self.operational_path, self.home, operational_fd, operational, parent_digest
                )
                or read_portable_source_plan(
                    self.home, record.plan.recipe.project_id, record.plan.content_digest
                )
                != record
                or source.capture() != first
            ):
                raise PolicyViolation("Local source authority durable verification failed")
            deadline.check()
            return verified
        except _SourceAuthorityReplay:
            if candidate is None:
                raise PolicyViolation("Local source authority replay unavailable") from None
            verified = self._classify(candidate)
            deadline.check()
            if verified is None:
                raise PolicyViolation("Local source authority replay verification failed") from None
            if (
                read_portable_source_plan(
                    self.home, record.plan.recipe.project_id, record.plan.content_digest
                )
                != record
                or operational_fd is None
                or not _source_authority_operational_unchanged(
                    self.operational_path, self.home, operational_fd, operational, parent_digest
                )
                or source.capture() != first
            ):
                raise PolicyViolation("Local source authority replay evidence drift") from None
            deadline.check()
            return verified
        except (OSError, sqlite3.Error) as exc:
            if commit_started and candidate is not None:
                recovered = self._classify(candidate)
                deadline.check()
                if recovered is not None:
                    try:
                        self._sync()
                    except OSError:
                        raise PolicyViolation(
                            "Local source authority durable sync failed"
                        ) from None
                    if (
                        read_portable_source_plan(
                            self.home, record.plan.recipe.project_id, record.plan.content_digest
                        )
                        != record
                        or operational_fd is None
                        or not _source_authority_operational_unchanged(
                            self.operational_path,
                            self.home,
                            operational_fd,
                            operational,
                            parent_digest,
                        )
                        or source.capture() != first
                    ):
                        raise PolicyViolation(
                            "Local source authority recovered evidence drift"
                        ) from None
                    deadline.check()
                    return recovered
            raise PolicyViolation("Local source authority write requires attention") from exc
        finally:
            _source_authority_cleanup(operational_db, operational_fd, side_fd, root_fd)

    def _classify(self, candidate: LocalBindingRevision) -> SourceAuthorityResult | None:
        try:
            self._preflight(create=False)
            with closing(_connect(self.path, readonly=True)) as db:
                db.execute("pragma busy_timeout=0")
                db.execute("begin")
                _validate(db)
                row = db.execute(
                    "select r.*,h.previous_generation,h.previous_revision_digest as head_previous "
                    "from local_source_binding_revision r join local_source_binding_head h "
                    "on h.device_id=r.device_id and h.source_binding_id=r.source_binding_id "
                    "and h.generation=r.generation and h.revision_digest=r.revision_digest "
                    "where r.revision_digest=?",
                    (candidate.revision_digest,),
                ).fetchone()
                if row is None:
                    return None
                if tuple(row)[:29] != _source_authority_revision_values(candidate) or tuple(row)[
                    29:
                ] != (
                    None if candidate.generation == 1 else candidate.generation - 1,
                    candidate.previous_revision_digest,
                ):
                    return None
                return SourceAuthorityResult(candidate.generation, candidate.revision_digest)
        except (OSError, sqlite3.Error, PolicyViolation):
            return None


def local_source_authority_path(home: Path) -> Path:
    return home / "yerel" / "source-authority.sqlite3"
