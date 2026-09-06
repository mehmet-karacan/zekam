"""Typed composition root for the local WP00-WP14 stores."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from zekam.application.composition import ApplicationContext
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError
from zekam.infrastructure.local_analytics import LocalAnalyticsStore
from zekam.infrastructure.local_file_security import (
    private_directory,
    private_regular,
    restrict_private_tree,
)
from zekam.infrastructure.sqlite.local_continuity_source_authority import (
    DDL_DIGEST as SOURCE_DDL_DIGEST,
)
from zekam.infrastructure.sqlite.local_continuity_source_authority import (
    SCHEMA_FINGERPRINT as SOURCE_SCHEMA_FINGERPRINT,
)
from zekam.infrastructure.sqlite.local_continuity_source_authority import SIDE_CAR_DDL
from zekam.infrastructure.sqlite.local_evidence_routing import (
    LOCAL_ROUTING_SCHEMA_DIGEST,
    SQLiteLocalEvidenceRouter,
)
from zekam.infrastructure.sqlite.local_improvement import (
    SCHEMA_DIGEST as IMPROVEMENT_SCHEMA_DIGEST,
)
from zekam.infrastructure.sqlite.local_improvement import SQLiteLocalImprovementStore
from zekam.infrastructure.sqlite.local_learning import SCHEMA_DIGEST as LEARNING_SCHEMA_DIGEST
from zekam.infrastructure.sqlite.local_learning import SCHEMA_VERSION as LEARNING_SCHEMA_VERSION
from zekam.infrastructure.sqlite.local_learning import SQLiteLocalLearning
from zekam.infrastructure.sqlite.local_model_benchmark import (
    SCHEMA_DIGEST as BENCHMARK_SCHEMA_DIGEST,
)
from zekam.infrastructure.sqlite.local_model_benchmark import SQLiteLocalBenchmarkLab
from zekam.infrastructure.sqlite.local_model_registry import (
    SCHEMA_DIGEST as REGISTRY_SCHEMA_DIGEST,
)
from zekam.infrastructure.sqlite.local_model_registry import SQLiteLocalModelRegistry
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import (
    SCHEMA_DIGEST as OPERATIONAL_SCHEMA_DIGEST,
)
from zekam.infrastructure.sqlite.operational_schema import (
    SCHEMA_VERSION as OPERATIONAL_SCHEMA_VERSION,
)
from zekam.infrastructure.sqlite.operational_schema import status as operational_status
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


def _schema_digest(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "select type,name,sql from sqlite_master where type in ('table','trigger') "
        "and name not like 'sqlite_%' order by type,name"
    ).fetchall()
    return digest([{"type": str(row[0]), "name": str(row[1]), "sql": str(row[2])} for row in rows])


with sqlite3.connect(":memory:") as _source_schema:
    _source_schema.executescript(SIDE_CAR_DDL)
    _SOURCE_SQLITE_SCHEMA_DIGEST = _schema_digest(_source_schema)


_LOCAL_CONTRACTS: dict[str, tuple[str, str, tuple[object, ...]]] = {
    "learning": (
        LEARNING_SCHEMA_DIGEST,
        "select singleton,version,schema_digest from learning_schema",
        (1, LEARNING_SCHEMA_VERSION, LEARNING_SCHEMA_DIGEST),
    ),
    "registry": (
        REGISTRY_SCHEMA_DIGEST,
        "select singleton,version from registry_schema",
        (1, 1),
    ),
    "benchmark": (
        BENCHMARK_SCHEMA_DIGEST,
        "select singleton,version from local_benchmark_schema",
        (1, 1),
    ),
    "routing": (
        LOCAL_ROUTING_SCHEMA_DIGEST,
        "select singleton,version from routing_schema",
        (1, 1),
    ),
    "improvement": (
        IMPROVEMENT_SCHEMA_DIGEST,
        "select singleton,version from improvement_schema",
        (1, 3),
    ),
}

_EXPECTED_DATABASE_DIGESTS = {
    "operational": OPERATIONAL_SCHEMA_DIGEST,
    "learning": LEARNING_SCHEMA_DIGEST,
    "registry": REGISTRY_SCHEMA_DIGEST,
    "benchmark": BENCHMARK_SCHEMA_DIGEST,
    "routing": LOCAL_ROUTING_SCHEMA_DIGEST,
    "improvement": IMPROVEMENT_SCHEMA_DIGEST,
    "source_authority": SOURCE_SCHEMA_FINGERPRINT,
}
_ANALYTICS_EMPTY_FINGERPRINT = digest(
    {
        "schema": "zekam-local-analytics-empty/v1",
        "directories": ["generations", "manifests", "raw", "receipts", "reports"],
    }
)


def validate_local_sqlite_store(
    name: str, path: Path, *, require_private_identity: bool = True
) -> dict[str, object]:
    """Reopen and verify one exact local schema, metadata row, FK graph and bytes."""

    info = path.lstat()
    if require_private_identity and not private_regular(path):
        raise ConfigurationError(f"Local {name} database identity invalid")
    if name == "operational":
        current = operational_status(path)
        if not (
            current.exists
            and current.integrity_ok
            and current.schema_ok
            and current.schema_version == OPERATIONAL_SCHEMA_VERSION
        ):
            raise ConfigurationError("Local operational schema validation failed")
        return {
            "schema_version": OPERATIONAL_SCHEMA_VERSION,
            "schema_digest": OPERATIONAL_SCHEMA_DIGEST,
        }
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.resolve(strict=True).as_uri()}?mode=ro", uri=True, timeout=5
        )
        connection.execute("pragma foreign_keys=on")
        connection.execute("pragma query_only=on")
        connection.execute("begin")
        integrity = connection.execute("pragma integrity_check").fetchall()
        if [str(row[0]) for row in integrity] != ["ok"]:
            raise ConfigurationError(f"Local {name} database integrity invalid")
        if connection.execute("pragma foreign_key_check").fetchone() is not None:
            raise ConfigurationError(f"Local {name} foreign key graph invalid")
        if name == "source_authority":
            page_size = connection.execute("pragma page_size").fetchone()[0]
            page_count = connection.execute("pragma page_count").fetchone()[0]
            meta = connection.execute(
                "select singleton,schema_version,schema_digest,created_at "
                "from local_source_authority_meta"
            ).fetchall()
            ledger = connection.execute(
                "select version,name,checksum,applied_at from local_source_authority_migration"
            ).fetchall()
            if (
                len(meta) != 1
                or tuple(meta[0])[:3] != (1, 1, SOURCE_SCHEMA_FINGERPRINT)
                or len(ledger) != 1
                or tuple(ledger[0]) != (1, "source-authority-v1", SOURCE_DDL_DIGEST, meta[0][3])
                or _schema_digest(connection) != _SOURCE_SQLITE_SCHEMA_DIGEST
                or page_size != 4096
                or page_count > 2048
                or info.st_size != page_size * page_count
            ):
                raise ConfigurationError("Local source authority schema validation failed")
            return {"schema_version": 1, "schema_digest": SOURCE_SCHEMA_FINGERPRINT}
        try:
            expected_digest, query, expected_row = _LOCAL_CONTRACTS[name]
        except KeyError as exc:
            raise ConfigurationError("Unknown local SQLite store contract") from exc
        rows = connection.execute(query).fetchall()
        if (
            len(rows) != 1
            or tuple(rows[0]) != expected_row
            or _schema_digest(connection) != expected_digest
        ):
            raise ConfigurationError(f"Local {name} schema validation failed")
        return {"schema_version": 1, "schema_digest": expected_digest}
    except (OSError, sqlite3.DatabaseError) as exc:
        raise ConfigurationError(f"Local {name} database validation failed") from exc
    finally:
        if connection is not None:
            connection.close()


@dataclass(frozen=True, slots=True)
class LocalCoreServices:
    """Every active local authority path, derived from one resolved home."""

    operational_path: Path
    operational: SQLiteOperationalStore
    runtime: SQLiteLocalRuntimeStore
    learning: SQLiteLocalLearning
    registry: SQLiteLocalModelRegistry
    benchmark: SQLiteLocalBenchmarkLab
    routing: SQLiteLocalEvidenceRouter
    analytics: LocalAnalyticsStore
    improvement: SQLiteLocalImprovementStore

    @classmethod
    def from_context(cls, context: ApplicationContext) -> LocalCoreServices:
        home = context.home
        operational_path = context.settings.database.sqlite_path(home)
        learning_path = home / "state" / "learning.db"
        registry_path = home / "modeller" / "registry" / "models.db"
        benchmark_path = home / "benchmarklar" / "benchmark.db"
        routing_path = home / "modeller" / "routing" / "routing.db"
        return cls(
            operational_path=operational_path,
            operational=SQLiteOperationalStore(operational_path),
            runtime=SQLiteLocalRuntimeStore(operational_path),
            learning=SQLiteLocalLearning(learning_path, operational_path=operational_path),
            registry=SQLiteLocalModelRegistry(registry_path),
            benchmark=SQLiteLocalBenchmarkLab(benchmark_path, home / "benchmarklar" / "artifacts"),
            routing=SQLiteLocalEvidenceRouter(
                routing_path, registry_path, benchmark_path, operational_path
            ),
            analytics=LocalAnalyticsStore(home / "analytics"),
            improvement=SQLiteLocalImprovementStore(
                home / "state" / "improvement.db", learning_path, benchmark_path
            ),
        )

    def bootstrap_extensions(self) -> None:
        """Create each additive store once; interrupted runs resume at the first missing store."""

        private_directories = (
            self.learning.path.parent,
            self.registry.path.parent,
            self.benchmark.path.parent,
            self.benchmark.artifact_root,
            self.routing.path.parent,
            self.analytics.root,
            self.analytics.raw,
            self.analytics.manifests,
            self.analytics.generations,
            self.analytics.quarantine,
            self.analytics.reports,
            self.analytics.receipts,
            self.improvement.path.parent,
        )
        for directory in private_directories:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ConfigurationError("Local service root directory olmali")
            directory.chmod(0o700)
        stores = (
            self.learning,
            self.registry,
            self.benchmark,
            self.routing,
            self.analytics,
            self.improvement,
        )
        for store in stores:
            missing = (
                not store.lock.is_file()
                if isinstance(store, LocalAnalyticsStore)
                else not store.path.is_file()
            )
            if missing:
                store.bootstrap()

    def status(self, *, semantic_analytics: bool = False) -> dict[str, object]:
        """Bounded read-only census; absence or corruption is never reported healthy."""

        files = {
            "operational": self.operational_path,
            "learning": self.learning.path,
            "registry": self.registry.path,
            "benchmark": self.benchmark.path,
            "routing": self.routing.path,
            "improvement": self.improvement.path,
            "source_authority": self.operational_path.parent.parent
            / "yerel"
            / "source-authority.sqlite3",
        }
        databases: dict[str, dict[str, object]] = {}
        for name, path in files.items():
            if not path.is_file() or path.is_symlink():
                databases[name] = {
                    "exists": False,
                    "identity_ok": False,
                    "integrity": False,
                    "schema_ok": False,
                    "schema_version": None,
                    "schema_digest": None,
                    "expected_schema_digest": _EXPECTED_DATABASE_DIGESTS[name],
                    "tables": 0,
                    "required": name != "source_authority",
                }
                continue
            identity_ok = private_regular(path)
            try:
                with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
                    tables = int(
                        db.execute(
                            "select count(*) from sqlite_master "
                            "where type='table' and name not like 'sqlite_%'"
                        ).fetchone()[0]
                    )
                contract = validate_local_sqlite_store(name, path, require_private_identity=False)
            except (OSError, ConfigurationError, sqlite3.DatabaseError):
                databases[name] = {
                    "exists": True,
                    "identity_ok": identity_ok,
                    "integrity": False,
                    "schema_ok": False,
                    "schema_version": None,
                    "schema_digest": None,
                    "expected_schema_digest": _EXPECTED_DATABASE_DIGESTS[name],
                    "tables": 0,
                    "required": name != "source_authority",
                }
            else:
                databases[name] = {
                    "exists": True,
                    "identity_ok": identity_ok,
                    "integrity": True,
                    "schema_ok": True,
                    **contract,
                    "expected_schema_digest": _EXPECTED_DATABASE_DIGESTS[name],
                    "tables": tables,
                    "required": name != "source_authority",
                }
        if semantic_analytics:
            analytics = self._analytics_status()
            analytics_ready = analytics["ready"] is True
        else:
            analytics_ready = self.analytics.root.is_dir() and all(
                path.is_dir()
                for path in (
                    self.analytics.raw,
                    self.analytics.manifests,
                    self.analytics.generations,
                    self.analytics.reports,
                )
            )
            analytics = {
                "exists": self.analytics.root.is_dir(),
                "ready": analytics_ready,
                "structure_ok": analytics_ready,
                "semantic_ok": None,
                "semantic_fingerprint": None,
                "empty_fingerprint": _ANALYTICS_EMPTY_FINGERPRINT,
                "state": "semantic-check-not-requested",
                "missing": [],
                "lock_exists": self.analytics.lock.is_file(),
                "repairable": analytics_ready,
                "required": True,
            }
        return {
            "schema": "zekam-local-core-status/v1",
            "databases": databases,
            "analytics": analytics,
            "analytics_ready": analytics_ready,
            "all_ready": analytics_ready
            and all(
                (not item["required"] and not item["exists"])
                or (
                    item["exists"]
                    and item["identity_ok"]
                    and item["integrity"]
                    and item["schema_ok"]
                )
                for item in databases.values()
            ),
            "grants_authority": False,
        }

    def _analytics_status(self) -> dict[str, object]:
        """Check empty/current analytics semantics without creating or rewriting bytes."""

        directories = (
            self.analytics.raw,
            self.analytics.manifests,
            self.analytics.generations,
            self.analytics.quarantine,
            self.analytics.reports,
            self.analytics.receipts,
        )
        required_paths = (self.analytics.root, *directories)
        try:
            existing_paths = tuple(path for path in required_paths if path.exists())
            missing_paths = tuple(path for path in required_paths if not path.exists())
            path_drift = any(not private_directory(path) for path in existing_paths)
            lock_exists = self.analytics.lock.exists() or self.analytics.lock.is_symlink()
            lock_drift = False
            if lock_exists:
                lock_drift = not private_regular(self.analytics.lock)
            structural_ready = (
                not missing_paths and lock_exists and not path_drift and not lock_drift
            )
            current_exists = self.analytics.current.exists() or self.analytics.current.is_symlink()
            source_present = any(
                any(path.iterdir())
                for path in (
                    self.analytics.raw,
                    self.analytics.manifests,
                    self.analytics.generations,
                    self.analytics.reports,
                    self.analytics.receipts,
                )
                if path.is_dir()
            )
            if current_exists:
                projection = self.analytics.current_projection()
                semantic_fingerprint = digest(projection)
                semantic_ok = True
                state = "current-projection"
            elif source_present:
                semantic_fingerprint = None
                semantic_ok = False
                state = "projection-missing"
            else:
                semantic_fingerprint = _ANALYTICS_EMPTY_FINGERPRINT
                semantic_ok = True
                state = "empty"
            ready = structural_ready and semantic_ok
            repairable = not path_drift and not lock_drift and semantic_ok
        except Exception:
            structural_ready = False
            semantic_ok = False
            semantic_fingerprint = None
            state = "invalid"
            ready = False
            repairable = False
            missing_paths = ()
            lock_exists = False
        return {
            "exists": self.analytics.root.is_dir() and not self.analytics.root.is_symlink(),
            "ready": ready,
            "structure_ok": structural_ready,
            "semantic_ok": semantic_ok,
            "semantic_fingerprint": semantic_fingerprint,
            "empty_fingerprint": _ANALYTICS_EMPTY_FINGERPRINT,
            "state": state,
            "missing": [path.name for path in missing_paths],
            "lock_exists": lock_exists,
            "identity_ok": not path_drift and not lock_drift,
            "repairable": repairable,
            "required": True,
        }

    def repair_plan(self) -> dict[str, object]:
        """Return an exact, authority-free plan for missing local extension stores."""

        snapshot = self.status(semantic_analytics=True)
        databases = snapshot["databases"]
        assert isinstance(databases, dict)
        missing = tuple(
            sorted(
                name
                for name, item in databases.items()
                if bool(item["required"]) and not bool(item["exists"])
            )
        )
        corrupt = tuple(
            sorted(
                name
                for name, item in databases.items()
                if bool(item["exists"])
                and bool(item["identity_ok"])
                and (not bool(item["integrity"]) or not bool(item["schema_ok"]))
            )
        )
        identity_drift = tuple(
            sorted(
                name
                for name, item in databases.items()
                if bool(item["exists"])
                and bool(item["integrity"])
                and bool(item["schema_ok"])
                and not bool(item["identity_ok"])
            )
        )
        blockers = corrupt + (("operational-missing",) if "operational" in missing else ())
        analytics = snapshot["analytics"]
        assert isinstance(analytics, dict)
        needs_analytics = not bool(snapshot["analytics_ready"])
        analytics_identity_drift = (
            needs_analytics
            and analytics.get("semantic_ok") is True
            and analytics.get("identity_ok") is False
            and not analytics.get("missing")
        )
        if needs_analytics and not bool(analytics["repairable"]) and not analytics_identity_drift:
            blockers += ("analytics",)
        action = (
            "restrict-private-local-tree"
            if not blockers and (identity_drift or analytics_identity_drift)
            else (
                "bootstrap-missing-local-stores"
                if not blockers and (missing or needs_analytics)
                else None
            )
        )
        body: dict[str, object] = {
            "schema": "zekam-local-core-repair-plan/v1",
            "action": action,
            "missing": list(missing),
            "identity_drift": list(identity_drift),
            "analytics_identity_drift": analytics_identity_drift,
            "analytics_missing": needs_analytics,
            "blocked_reasons": list(blockers),
            "before": snapshot,
            "grants_authority": False,
        }
        return {**body, "plan_digest": digest(body)}

    def apply_repair(self, plan_digest: str) -> dict[str, object]:
        """Apply exactly one current local plan and verify every resulting store."""

        plan = self.repair_plan()
        if plan["plan_digest"] != plan_digest:
            raise ConfigurationError("Yerel doctor repair plan digest stale veya farkli")
        if plan["blocked_reasons"]:
            raise ConfigurationError("Yerel doctor repair bozuk/mevcut authority nedeniyle bloke")
        action = plan["action"]
        if action not in {"bootstrap-missing-local-stores", "restrict-private-local-tree"}:
            raise ConfigurationError("Yerel doctor repair planinda uygulanacak adim yok")
        if action == "restrict-private-local-tree":
            home = self.operational_path.parent.parent.resolve(strict=True)
            if home == Path(home.anchor) or home.is_symlink():
                raise ConfigurationError("Yerel doctor ACL repair bounded home ister")
            restrict_private_tree(home)
        else:
            self.bootstrap_extensions()
        after = self.status(semantic_analytics=True)
        if not after["all_ready"]:
            raise ConfigurationError("Yerel doctor repair sonrasi butunluk dogrulanamadi")
        receipt_body = {
            "schema": "zekam-local-core-repair-receipt/v1",
            "step": action,
            "plan_digest": plan_digest,
            "before": plan["before"],
            "after": after,
        }
        return {**receipt_body, "receipt_id": digest(receipt_body), "grants_authority": False}
