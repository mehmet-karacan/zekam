"""Cross-platform local client discovery and routing eligibility registry."""

# ruff: noqa: E501 -- literal SQLite DDL remains directly reviewable.

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import final

from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_file_security import (
    owned_regular,
    private_directory,
    private_regular,
    restrict_private_tree,
)

MAX_MODELS = 4096
MAX_OUTPUT = 1_048_576
MAX_CLIENT_ARTIFACT_BYTES = 512 * 1024 * 1024
SCHEMA_DIGEST = "sha256:b217ea3533458258e75c708360dc34d7e36620c95910a757212c7691ce00852f"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_SECRET = re.compile(
    r"(?i)(?:\b(?:bearer|password|secret|credential|api[-_]?key)\b|"
    r"\b(?:sk|pk)[-_][A-Za-z0-9]{8,}|://)"
)

_SCHEMA = r"""
pragma foreign_keys=on;
create table registry_schema(singleton integer primary key check(singleton=1),version integer not null);
insert into registry_schema values(1,1);
create table discovery_snapshot(
 snapshot_digest text primary key,device_id text not null,client_id text not null,
 client_version text not null,listing_supported integer not null check(listing_supported in(0,1)),
 observed_at text not null,expires_at text not null,body_json text not null
) strict;
create table discovery_observation(
 snapshot_digest text not null references discovery_snapshot,ordinal integer not null,
 provider_id text not null,model_id text not null,exact_id text not null,
 revision_fingerprint text not null,body_json text not null,
 primary key(snapshot_digest,ordinal)
) strict;
create table reconcile_event(
 event_digest text primary key,snapshot_digest text not null references discovery_snapshot,
 exact_id text not null,disposition text not null,
 prior_fingerprint text,new_fingerprint text,body_json text not null,
 check(disposition in('new','removed','changed','unchanged','ambiguous')),
 unique(snapshot_digest,exact_id)
) strict;
create table health_observation(
 health_digest text primary key,snapshot_digest text not null references discovery_snapshot,
 exact_id text not null,revision_fingerprint text not null,status text not null,
 evidence_digest text not null,observed_at text not null,body_json text not null,
 check(status in('passed','failed')),unique(snapshot_digest,exact_id,evidence_digest)
) strict;
create table local_profile_report(
 report_digest text primary key,snapshot_digest text not null references discovery_snapshot,
 created_at text not null,body_json text not null
) strict;
"""

for _table in (
    "discovery_snapshot",
    "discovery_observation",
    "reconcile_event",
    "health_observation",
    "local_profile_report",
):
    _SCHEMA += f"create trigger {_table}_no_update before update on {_table} begin select raise(abort,'append-only'); end;\n"
    _SCHEMA += f"create trigger {_table}_no_delete before delete on {_table} begin select raise(abort,'append-only'); end;\n"


def _text(value: object, label: str, maximum: int = 256) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > maximum
        or _SECRET.search(value)
    ):
        raise ValidationFailed(f"Local model {label} is invalid or sensitive")
    return value


def _instant(value: dt.datetime) -> str:
    if type(value) is not dt.datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailed("Local model timestamp must be timezone-aware")
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()


def _parse_instant(value: object) -> dt.datetime:
    if type(value) is not str:
        raise PolicyViolation("Stored local model timestamp type drift")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise PolicyViolation("Stored local model timestamp drift") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PolicyViolation("Stored local model timestamp lacks timezone")
    return parsed.astimezone(dt.UTC)


def _schema_digest(db: sqlite3.Connection) -> str:
    rows = db.execute(
        "select type,name,sql from sqlite_master where type in ('table','trigger') "
        "and name not like 'sqlite_%' order by type,name"
    ).fetchall()
    return digest([{"type": str(row[0]), "name": str(row[1]), "sql": str(row[2])} for row in rows])


@dataclass(frozen=True, slots=True)
class LocalModelIdentity:
    provider_id: str
    model_id: str
    revision: str

    def __post_init__(self) -> None:
        if not _ID.fullmatch(_text(self.provider_id, "provider id", 128)):
            raise ValidationFailed("Local model provider id invalid")
        if not _MODEL.fullmatch(_text(self.model_id, "model id", 256)):
            raise ValidationFailed("Local model id invalid")
        _text(self.revision, "revision", 256)

    @property
    def exact_id(self) -> str:
        return f"{self.provider_id}/{self.model_id}"

    @property
    def revision_fingerprint(self) -> str:
        return digest(
            {
                "schema": "zekam-local-model-identity/v1",
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "revision": self.revision,
            }
        )

    def body(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "revision": self.revision,
            "exact_id": self.exact_id,
            "revision_fingerprint": self.revision_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class LocalDiscoverySnapshot:
    device_id: str
    client_id: str
    client_version: str
    client_artifact_digest: str
    listing_supported: bool
    models: tuple[LocalModelIdentity, ...]
    observed_at: dt.datetime
    expires_at: dt.datetime

    def __post_init__(self) -> None:
        _text(self.device_id, "device id", 128)
        if self.client_id not in {"opencode", "codex"}:
            raise ValidationFailed("Unsupported local model client")
        _text(self.client_version, "client version", 128)
        parse_digest(self.client_artifact_digest)
        if type(self.listing_supported) is not bool:
            raise ValidationFailed("Listing support must be bool")
        _instant(self.observed_at)
        _instant(self.expires_at)
        if not self.observed_at < self.expires_at <= self.observed_at + dt.timedelta(days=7):
            raise ValidationFailed("Local discovery TTL invalid")
        if type(self.models) is not tuple or len(self.models) > MAX_MODELS:
            raise ValidationFailed("Local discovery model set invalid")
        if not self.listing_supported and self.models:
            raise ValidationFailed("Unsupported listing cannot carry guessed models")
        for model in self.models:
            if type(model) is not LocalModelIdentity:
                raise ValidationFailed("Exact local model identity required")

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-local-model-discovery/v1",
            "device_id": self.device_id,
            "client_id": self.client_id,
            "client_version": self.client_version,
            "client_artifact_digest": self.client_artifact_digest,
            "listing_supported": self.listing_supported,
            "models": [
                model.body()
                for model in sorted(
                    self.models,
                    key=lambda item: (item.exact_id, item.revision_fingerprint),
                )
            ],
            "observed_at": _instant(self.observed_at),
            "expires_at": _instant(self.expires_at),
            "contains_secrets": False,
            "grants_routing_authority": False,
        }

    @property
    def snapshot_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class LocalModelCapabilityProfile:
    snapshot_digest: str
    exact_id: str
    revision_fingerprint: str
    family_id: str
    execution_identity: str
    workloads: tuple[str, ...]
    modalities: tuple[str, ...]
    data_classifications: tuple[str, ...]
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        parse_digest(self.snapshot_digest)
        parse_digest(self.revision_fingerprint)
        for value, label in (
            (self.exact_id, "capability exact id"),
            (self.family_id, "capability family"),
            (self.execution_identity, "capability execution identity"),
        ):
            _text(value, label, 385)
        collections = (
            self.workloads,
            self.modalities,
            self.data_classifications,
            self.capabilities,
        )
        if any(
            type(items) is not tuple
            or not items
            or len(items) > 32
            or tuple(sorted(set(items))) != items
            or any(type(item) is not str or not _ID.fullmatch(item) for item in items)
            for items in collections
        ):
            raise ValidationFailed("Local model capability profile scopes invalid")

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-local-model-capability-profile/v1",
            "snapshot_digest": self.snapshot_digest,
            "exact_id": self.exact_id,
            "revision_fingerprint": self.revision_fingerprint,
            "family_id": self.family_id,
            "execution_identity": self.execution_identity,
            "workloads": list(self.workloads),
            "modalities": list(self.modalities),
            "data_classifications": list(self.data_classifications),
            "capabilities": list(self.capabilities),
            "grants_routing_authority": False,
        }

    @property
    def profile_digest(self) -> str:
        return digest(self.body())


def parse_opencode_models(output: bytes, *, revision: str) -> tuple[LocalModelIdentity, ...]:
    if type(output) is not bytes or not 0 < len(output) <= MAX_OUTPUT:
        raise ValidationFailed("OpenCode model output size invalid")
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("OpenCode model output must be UTF-8") from exc
    if _SECRET.search(text):
        raise PolicyViolation("OpenCode discovery output contains sensitive material")
    lines = text.splitlines()
    if (
        not lines
        or len(lines) > MAX_MODELS
        or any(not line or line != line.strip() for line in lines)
    ):
        raise ValidationFailed("OpenCode model list invalid")
    records: list[LocalModelIdentity] = []
    for line in lines:
        provider, separator, model = line.partition("/")
        if not separator:
            raise ValidationFailed("OpenCode model identity must be exact provider/model")
        records.append(LocalModelIdentity(provider, model, revision))
    return tuple(records)


def discover_installed_client(
    executable: Path,
    *,
    client_id: str,
    device_id: str,
    private_root: Path,
    expected_artifact_digest: str,
    now: dt.datetime,
    config_root: Path | None = None,
) -> LocalDiscoverySnapshot:
    """Read local CLI inventory only; never refresh or invoke a model."""
    if client_id not in {"opencode", "codex"}:
        raise ValidationFailed("Unsupported discovery client")
    parse_digest(expected_artifact_digest)
    for path, label in ((executable, "executable"), (private_root, "private root")):
        if not path.is_absolute() or path.is_symlink():
            raise ValidationFailed(f"Local discovery {label} path invalid")
    if config_root is not None and (
        not config_root.is_absolute()
        or config_root.is_symlink()
        or not private_directory(config_root)
    ):
        raise PolicyViolation("Local discovery config root identity invalid")
    info = executable.stat()
    if not owned_regular(executable) or not private_directory(private_root):
        raise PolicyViolation("Local discovery path identity invalid")
    if info.st_size <= 0 or info.st_size > MAX_CLIENT_ARTIFACT_BYTES:
        raise PolicyViolation("Local discovery artifact size invalid")
    artifact_hash = hashlib.sha256()
    with executable.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            artifact_hash.update(chunk)
    artifact_digest = "sha256:" + artifact_hash.hexdigest()
    if artifact_digest != expected_artifact_digest:
        raise PolicyViolation("Local discovery artifact digest drift")
    environment = {
        "HOME": str(private_root),
        "XDG_CONFIG_HOME": str(config_root or (private_root / "config")),
        "XDG_CACHE_HOME": str(private_root / "cache"),
        "XDG_DATA_HOME": str(private_root / "data"),
        "NO_COLOR": "1",
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    if os.name == "nt":
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        environment.update(
            {
                "USERPROFILE": str(private_root),
                "APPDATA": str(private_root / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(private_root / "AppData" / "Local"),
                "TEMP": str(private_root / "Temp"),
                "TMP": str(private_root / "Temp"),
                "SYSTEMROOT": system_root,
                "WINDIR": system_root,
                "COMSPEC": os.environ.get("COMSPEC", str(Path(system_root) / "System32/cmd.exe")),
                "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
                "PATH": os.pathsep.join(
                    (
                        str(executable.parent),
                        str(Path(system_root) / "System32"),
                        system_root,
                    )
                ),
            }
        )
    command = [str(executable), "--version"]
    version_run = subprocess.run(
        command, env=environment, capture_output=True, check=False, timeout=10
    )
    if (
        version_run.returncode != 0
        or len(version_run.stdout) > 4096
        or len(version_run.stderr) > 4096
        or _SECRET.search(version_run.stderr.decode("utf-8", errors="replace"))
    ):
        raise PolicyViolation("Local client version discovery failed")
    version = _text(version_run.stdout.decode("utf-8").strip(), "client version", 128)
    models: tuple[LocalModelIdentity, ...] = ()
    supported = client_id == "opencode"
    if supported:
        model_run = subprocess.run(
            [str(executable), "models", "--pure"],
            env=environment,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if (
            model_run.returncode != 0
            or len(model_run.stderr) > 4096
            or _SECRET.search(model_run.stderr.decode("utf-8", errors="replace"))
        ):
            raise PolicyViolation("OpenCode local model discovery failed")
        models = parse_opencode_models(model_run.stdout, revision=version)
    after = executable.stat()
    after_hash = hashlib.sha256()
    with executable.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            after_hash.update(chunk)
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ) or "sha256:" + after_hash.hexdigest() != artifact_digest:
        raise PolicyViolation("Local discovery artifact changed during observation")
    return LocalDiscoverySnapshot(
        device_id,
        client_id,
        version,
        artifact_digest,
        supported,
        models,
        now,
        now + dt.timedelta(hours=24),
    )


@final
class SQLiteLocalModelRegistry:
    def __init__(self, path: Path) -> None:
        if not path.is_absolute() or path.is_symlink():
            raise ValidationFailed("Local model registry path invalid")
        self.path = path

    def bootstrap(self) -> None:
        created = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created:
            restrict_private_tree(self.path.parent)
        if not private_directory(self.path.parent):
            raise PolicyViolation("Local model registry private parent required")
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript(_SCHEMA)
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        if not private_regular(self.path):
            raise PolicyViolation("Local model registry identity invalid")
        db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys=on")
        db.execute("pragma busy_timeout=5000")
        if (
            db.execute("select version from registry_schema").fetchone()[0] != 1
            or _schema_digest(db) != SCHEMA_DIGEST
        ):
            db.close()
            raise PolicyViolation("Local model registry schema drift")
        return db

    def reconcile(self, snapshot: LocalDiscoverySnapshot) -> dict[str, int]:
        if type(snapshot) is not LocalDiscoverySnapshot:
            raise ValidationFailed("Exact local discovery snapshot required")
        snapshot.__post_init__()
        raw = canonical_json(snapshot.body())
        groups: dict[str, list[LocalModelIdentity]] = defaultdict(list)
        for model in snapshot.models:
            groups[model.exact_id].append(model)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            replay = db.execute(
                "select body_json from discovery_snapshot where snapshot_digest=?",
                (snapshot.snapshot_digest,),
            ).fetchone()
            if replay is not None:
                if replay[0] != raw:
                    raise PolicyViolation("Local discovery replay drift")
                db.rollback()
                return self._counts(db, snapshot.snapshot_digest)
            prior = db.execute(
                "select snapshot_digest,observed_at from discovery_snapshot "
                "where device_id=? and client_id=? order by observed_at desc limit 1",
                (snapshot.device_id, snapshot.client_id),
            ).fetchone()
            if prior is not None and _parse_instant(prior["observed_at"]) >= snapshot.observed_at:
                raise PolicyViolation("Local discovery stale snapshot")
            previous: dict[str, str] = {}
            if prior is not None:
                rows = db.execute(
                    "select exact_id,new_fingerprint from reconcile_event "
                    "where snapshot_digest=? and disposition not in('removed','ambiguous')",
                    (prior["snapshot_digest"],),
                ).fetchall()
                previous = {str(row[0]): str(row[1]) for row in rows}
            db.execute(
                "insert into discovery_snapshot values(?,?,?,?,?,?,?,?)",
                (
                    snapshot.snapshot_digest,
                    snapshot.device_id,
                    snapshot.client_id,
                    snapshot.client_version,
                    int(snapshot.listing_supported),
                    _instant(snapshot.observed_at),
                    _instant(snapshot.expires_at),
                    raw,
                ),
            )
            for ordinal, model in enumerate(snapshot.models, 1):
                db.execute(
                    "insert into discovery_observation values(?,?,?,?,?,?,?)",
                    (
                        snapshot.snapshot_digest,
                        ordinal,
                        model.provider_id,
                        model.model_id,
                        model.exact_id,
                        model.revision_fingerprint,
                        canonical_json(model.body()),
                    ),
                )
            for exact_id in sorted(set(previous) | set(groups)):
                found = groups.get(exact_id, [])
                if len(found) > 1:
                    disposition, new = "ambiguous", None
                elif not found:
                    disposition, new = "removed", None
                else:
                    new = found[0].revision_fingerprint
                    disposition = (
                        "new"
                        if exact_id not in previous
                        else "unchanged"
                        if previous[exact_id] == new
                        else "changed"
                    )
                event = {
                    "schema": "zekam-local-model-reconcile/v1",
                    "snapshot_digest": snapshot.snapshot_digest,
                    "exact_id": exact_id,
                    "disposition": disposition,
                    "prior_fingerprint": previous.get(exact_id),
                    "new_fingerprint": new,
                }
                db.execute(
                    "insert into reconcile_event values(?,?,?,?,?,?,?)",
                    (
                        digest(event),
                        snapshot.snapshot_digest,
                        exact_id,
                        disposition,
                        previous.get(exact_id),
                        new,
                        canonical_json(event),
                    ),
                )
            db.commit()
            return self._counts(db, snapshot.snapshot_digest)

    @staticmethod
    def _counts(db: sqlite3.Connection, snapshot_digest: str) -> dict[str, int]:
        return {
            str(row[0]): int(row[1])
            for row in db.execute(
                "select disposition,count(*) from reconcile_event "
                "where snapshot_digest=? group by disposition",
                (snapshot_digest,),
            ).fetchall()
        }

    def record_health(
        self,
        snapshot_digest: str,
        exact_id: str,
        revision_fingerprint: str,
        *,
        passed: bool,
        evidence_digest: str,
        now: dt.datetime,
    ) -> str:
        parse_digest(snapshot_digest)
        parse_digest(revision_fingerprint)
        parse_digest(evidence_digest)
        _text(exact_id, "exact id", 385)
        if type(passed) is not bool:
            raise ValidationFailed("Local health status must be bool")
        body = {
            "schema": "zekam-local-model-health/v1",
            "snapshot_digest": snapshot_digest,
            "exact_id": exact_id,
            "revision_fingerprint": revision_fingerprint,
            "status": "passed" if passed else "failed",
            "evidence_digest": evidence_digest,
            "observed_at": _instant(now),
            "contains_response": False,
        }
        raw, value = canonical_json(body), digest(body)
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            valid = db.execute(
                "select s.observed_at,s.expires_at from reconcile_event r "
                "join discovery_snapshot s on s.snapshot_digest=r.snapshot_digest "
                "where r.snapshot_digest=? and r.exact_id=? and r.new_fingerprint=? "
                "and r.disposition not in('removed','ambiguous')",
                (snapshot_digest, exact_id, revision_fingerprint),
            ).fetchone()
            if valid is None:
                raise PolicyViolation("Health observation exact current model required")
            observed = _parse_instant(valid["observed_at"])
            expires = _parse_instant(valid["expires_at"])
            if not observed <= now.astimezone(dt.UTC) < expires:
                raise PolicyViolation("Health observation outside discovery lifetime")
            existing = db.execute(
                "select health_digest,body_json from health_observation "
                "where snapshot_digest=? and exact_id=? and evidence_digest=?",
                (snapshot_digest, exact_id, evidence_digest),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (value, raw):
                    raise PolicyViolation("Health observation evidence payload drift")
                db.rollback()
                return value
            db.execute(
                "insert into health_observation values(?,?,?,?,?,?,?,?)",
                (
                    value,
                    snapshot_digest,
                    exact_id,
                    revision_fingerprint,
                    body["status"],
                    evidence_digest,
                    _instant(now),
                    raw,
                ),
            )
            db.commit()
        return value

    def record_capability_profile(
        self, profile: LocalModelCapabilityProfile, *, now: dt.datetime
    ) -> str:
        if type(profile) is not LocalModelCapabilityProfile:
            raise ValidationFailed("Exact local model capability profile required")
        profile.__post_init__()
        body = profile.body()
        raw, value = canonical_json(body), profile.profile_digest
        with closing(self._connect()) as db:
            db.execute("begin immediate")
            current = db.execute(
                "select 1 from reconcile_event where snapshot_digest=? and exact_id=? "
                "and new_fingerprint=? and disposition not in('removed','ambiguous')",
                (profile.snapshot_digest, profile.exact_id, profile.revision_fingerprint),
            ).fetchone()
            if current is None:
                raise PolicyViolation("Capability profile requires exact discovered model")
            rows = db.execute(
                "select report_digest,body_json from local_profile_report "
                "where snapshot_digest=? and json_extract(body_json,'$.schema')=? "
                "and json_extract(body_json,'$.exact_id')=?",
                (
                    profile.snapshot_digest,
                    "zekam-local-model-capability-profile/v1",
                    profile.exact_id,
                ),
            ).fetchall()
            if rows:
                if len(rows) != 1 or tuple(rows[0]) != (value, raw):
                    raise PolicyViolation("Local model capability profile conflict")
                db.rollback()
                return value
            db.execute(
                "insert into local_profile_report values(?,?,?,?)",
                (value, profile.snapshot_digest, _instant(now), raw),
            )
            db.commit()
        return value

    def routable(self, *, device_id: str, client_id: str, now: dt.datetime) -> tuple[str, ...]:
        _text(device_id, "device id", 128)
        if client_id not in {"opencode", "codex"}:
            raise ValidationFailed("Unsupported local model client")
        moment = _instant(now)
        with closing(self._connect()) as db:
            snapshot = db.execute(
                "select * from discovery_snapshot where device_id=? and client_id=? "
                "order by observed_at desc limit 1",
                (device_id, client_id),
            ).fetchone()
            if (
                snapshot is None
                or not snapshot["listing_supported"]
                or moment >= snapshot["expires_at"]
            ):
                return ()
            rows = db.execute(
                "select r.exact_id,r.new_fingerprint from reconcile_event r "
                "where r.snapshot_digest=? and r.disposition not in('removed','ambiguous') "
                "order by r.exact_id",
                (snapshot["snapshot_digest"],),
            ).fetchall()
            result: list[str] = []
            for row in rows:
                history = db.execute(
                    "select status,observed_at from health_observation "
                    "where snapshot_digest=? and exact_id=? and revision_fingerprint=? "
                    "and observed_at<=? order by observed_at desc limit 3",
                    (
                        snapshot["snapshot_digest"],
                        row["exact_id"],
                        row["new_fingerprint"],
                        moment,
                    ),
                ).fetchall()
                if not history or history[0]["status"] != "passed":
                    continue
                if now - _parse_instant(history[0]["observed_at"]) > dt.timedelta(hours=24):
                    continue
                if history[0]["status"] == "failed":
                    continue
                if (
                    len(history) == 3
                    and history[1]["status"] == history[2]["status"] == "failed"
                    and now - _parse_instant(history[1]["observed_at"]) < dt.timedelta(minutes=5)
                ):
                    continue
                result.append(str(row["exact_id"]))
            return tuple(result)

    def profile(self, snapshot_digest: str, *, now: dt.datetime) -> str:
        parse_digest(snapshot_digest)
        with closing(self._connect()) as db:
            snapshot = db.execute(
                "select device_id,client_id from discovery_snapshot where snapshot_digest=?",
                (snapshot_digest,),
            ).fetchone()
            if snapshot is None:
                raise PolicyViolation("Local profile snapshot missing")
            counts = self._counts(db, snapshot_digest)
            body = {
                "schema": "zekam-local-model-profile/v1",
                "snapshot_digest": snapshot_digest,
                "device_id": snapshot["device_id"],
                "client_id": snapshot["client_id"],
                "reconcile_counts": counts,
                "created_at": _instant(now),
                "contains_secrets": False,
                "grants_routing_authority": False,
            }
            raw, value = canonical_json(body), digest(body)
            db.execute(
                "insert or ignore into local_profile_report values(?,?,?,?)",
                (value, snapshot_digest, _instant(now), raw),
            )
            db.commit()
        return value
