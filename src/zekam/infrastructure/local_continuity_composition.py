"""Explicit existing-state lifecycle commands, without client hook activation.

Current operations retain full environment/source admission. Historical diagnostics
and post-freeze control observations use a separate immutable-evidence path; they
cannot hydrate, compile, repair, or create execution authority.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.config import core_root
from zekam.application.fresh_bootstrap import OPERATIONAL_RELATIVE_PATH
from zekam.application.home import assert_separated_from_core
from zekam.application.knowledge_plane_service import KnowledgePlaneService
from zekam.application.local_continuity import (
    ContinuityBinding,
    bounded_int,
    digest_text,
    logical,
    uuid_text,
)
from zekam.application.local_continuity_close import (
    CANDIDATE_RECIPE_DIGEST,
    CloseCandidateBundle,
    CloseSummary,
    FrozenClose,
    LocalCloseService,
)
from zekam.application.local_continuity_source_plan import ContinuitySourceRecipe
from zekam.application.local_continuity_startup import StartupRequest
from zekam.domain.errors import PolicyViolation, ValidationFailed, ZekamError
from zekam.infrastructure.clients.local_continuity_decoder import validate_reviewed_control_entry
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.local_continuity_environment import (
    LocalContinuityEnvironment,
    _path_identity,
)
from zekam.infrastructure.local_continuity_source_plan import BoundedContinuitySource
from zekam.infrastructure.local_startup_composition import (
    LocalStartupComposition,
    compose_local_startup,
)
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_close import SQLiteCloseStore
from zekam.infrastructure.sqlite.local_continuity_control import SQLiteContinuityControlStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


@dataclass(frozen=True, slots=True)
class LocalContinuityArguments:
    home: Path
    session_id: str
    source_root: Path
    source_paths: tuple[str, ...]
    index_path: Path | None = None

    def __post_init__(self) -> None:
        for path in (self.home, self.source_root, self.index_path):
            if path is not None and (
                not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts
            ):
                raise ValidationFailed("Local continuity canonical absolute paths required")
        if self.home is None or self.source_root is None:
            raise ValidationFailed("Local continuity home and source root required")
        uuid_text(self.session_id, "Local continuity session")
        if not isinstance(self.source_paths, tuple) or not 1 <= len(self.source_paths) <= 8:
            raise ValidationFailed("Local continuity requires 1..8 explicit source paths")
        if any(not isinstance(path, str) or not path for path in self.source_paths):
            raise ValidationFailed("Local continuity source paths must be nonempty text")


def _report(operation: str, **body: Any) -> dict[str, Any]:
    return {
        "schema": "zekam-local-continuity-command/v1",
        "operation": operation,
        **body,
        "installed_client_lifecycle_proven": False,
        "native_ack": False,
        "grants_authority": False,
    }


class LocalContinuityRuntime:
    """Only an existing exact session can be the target of these commands."""

    def __init__(self, arguments: LocalContinuityArguments) -> None:
        if not isinstance(arguments, LocalContinuityArguments):
            raise ValidationFailed("Typed local continuity arguments required")
        arguments.__post_init__()
        self.arguments = arguments
        self.path = arguments.home / OPERATIONAL_RELATIVE_PATH
        assert_separated_from_core(arguments.home, core_root())
        _path_identity(arguments.home, directory=True)
        before = _path_identity(self.path)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _path_identity(sidecar)
        self.base = SQLiteContinuityStore(self.path)
        with self._read() as db:
            row = db.execute(
                "select * from continuity_session_binding where session_id=?",
                (arguments.session_id,),
            ).fetchone()
            if row is None:
                raise PolicyViolation("Local continuity requires an existing session binding")
            self.binding = ContinuityBinding(
                **{field.name: row[field.name] for field in fields(ContinuityBinding)}
            )
            SQLiteContinuityControlStore._historical_binding(db, self.binding)
            source = db.execute(
                "select b.id,b.source_kind from source_snapshot s"
                " join source_binding b on b.id=s.source_binding_id where s.id=?",
                (self.binding.source_snapshot_id,),
            ).fetchone()
            if source is None or source["source_kind"] != "git":
                raise PolicyViolation("Local continuity requires an admitted bounded Git snapshot")
            self.recipe = ContinuitySourceRecipe(
                self.binding.project_id,
                self.binding.realm_id,
                source["id"],
                arguments.source_paths,
                self.binding.task_digest,
                self.binding.policy_digest,
            )
        after = _path_identity(self.path)
        # Canonical bytes may change under another legitimate writer, not file identity.
        if before[:-6] != after[:-6] or before[-6:-3] != after[-6:-3]:
            raise PolicyViolation("Local continuity operational path identity drift")
        if self.binding.client_id not in {"codex", "claude-code"}:
            raise PolicyViolation("Local continuity client has no reviewed structural decoder")
        self.environment = LocalContinuityEnvironment(
            arguments.home, core_root(), core_root() / "AKTIF_GOREV.md", self.path
        )
        self.spool = ClientLifecycleSpool(arguments.home, client_id=self.binding.client_id)
        self.controls = SQLiteContinuityControlStore(self.base, self.spool)

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        # A separate read-only connection also works while a current operation holds a writer.
        connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        with closing(connection):
            connection.row_factory = sqlite3.Row
            connection.execute("pragma query_only=on")
            connection.execute("begin")
            yield connection

    def _source(self) -> BoundedContinuitySource:
        return BoundedContinuitySource(self.arguments.source_root, self.recipe)

    def _check_current(self) -> None:
        self.environment.validate(self.binding)
        self._source().probe(SQLiteOperationalStore(self.path), self.binding)

    @contextmanager
    def _current(self) -> Iterator[LocalStartupComposition]:
        self._check_current()
        index = None
        if self.arguments.index_path is not None:
            index = SQLiteKnowledgeIndex(self.arguments.index_path, read_only=True)
        try:
            composed = compose_local_startup(
                self.environment, self.binding, self._source(), index=index
            )
            if not isinstance(composed.lifecycle.store, SQLiteContinuityStore):
                raise PolicyViolation("Local continuity requires exact operational adapter")
            composed.lifecycle.controls = SQLiteContinuityControlStore(
                composed.lifecycle.store, composed.lifecycle.spool
            )
            yield composed
        finally:
            if index is not None:
                index.close()

    def drain(self) -> dict[str, Any]:
        if self.controls.is_frozen(self.binding):
            count = self.controls.drain(self.binding)
            scope = "historical-control-only"
        else:
            with self._current() as current:
                count = current.drain()
            scope = "current-source-and-spool"
        return _report("drain", state="persisted", persisted_spool_count=count, scope=scope)

    def hydrate(self, request: StartupRequest) -> dict[str, Any]:
        if not isinstance(request, StartupRequest):
            raise ValidationFailed("Typed local startup request required")
        request.__post_init__()
        with self._current() as current:
            return current.hydrate(request)

    def checkpoint(self, context_digest: str, key: str) -> dict[str, Any]:
        digest_text(context_digest)
        logical(key, "Local checkpoint key")
        with self._current() as current:
            value = current.lifecycle.pre_compaction(context_digest=context_digest, key=key)
        return _report("checkpoint", state="checkpointed", checkpoint_digest=value)

    def resume(self, checkpoint_digest: str) -> dict[str, Any]:
        digest_text(checkpoint_digest)
        with self._current() as composed:
            result = composed.lifecycle.store.resume(self.binding, checkpoint_digest)
        return result | {"installed_client_lifecycle_proven": False, "native_ack": False}

    @contextmanager
    def _close(
        self,
    ) -> Iterator[tuple[LocalStartupComposition, SQLiteCloseStore, LocalCloseService]]:
        with self._current() as composed:
            with self._read() as db:
                config = db.execute(
                    "select max_pending_outbox from local_runtime_config where singleton=1"
                ).fetchone()
                if config is None or type(config[0]) is not int or not 1 <= config[0] <= 100000:
                    raise PolicyViolation(
                        "Local continuity requires existing admitted runtime config"
                    )
            runtime = SQLiteLocalRuntimeStore(self.path, existing_only=True)
            files = KnowledgeFileStore(self.arguments.home)

            def current(binding: ContinuityBinding) -> None:
                if binding != self.binding:
                    raise PolicyViolation("Local close exact owner required")
                composed.sources.preflight(binding)

            if not isinstance(composed.lifecycle.store, SQLiteContinuityStore):
                raise PolicyViolation("Local continuity requires exact operational adapter")
            store = SQLiteCloseStore(composed.lifecycle.store, runtime, files, source_probe=current)
            service = LocalCloseService(
                store,
                runtime,
                KnowledgePlaneService(SQLiteOperationalStore(self.path), files),
                source_probe=current,
                verify_projection=store.verify_projection,
            )
            yield composed, store, service

    @staticmethod
    def _request_result(operation: str, request: FrozenClose) -> dict[str, Any]:
        return _report(
            operation,
            state=request.state,
            request_digest=request.request_digest,
            job_id=request.job_id,
            outbox_id=request.outbox_id,
        )

    def freeze(self, summary: CloseSummary, context_digest: str, key: str) -> dict[str, Any]:
        if not isinstance(summary, CloseSummary):
            raise ValidationFailed("Typed local close summary required")
        summary.__post_init__()
        digest_text(context_digest)
        logical(key, "Local close key")
        with self._close() as (composed, store, _):
            value = composed.lifecycle.pre_close(
                store, summary, context_digest=context_digest, key=key
            )
        return self._request_result("freeze", value)

    def freeze_v2(
        self,
        summary: CloseSummary,
        candidates: CloseCandidateBundle,
        context_digest: str,
        key: str,
    ) -> dict[str, Any]:
        if type(summary) is not CloseSummary or type(candidates) is not CloseCandidateBundle:
            raise ValidationFailed("Exact typed local v2 close summary and candidates required")
        summary.__post_init__()
        candidates.__post_init__()
        digest_text(context_digest)
        logical(key, "Local close key")
        with self._close() as (composed, store, _):
            value = composed.lifecycle.pre_close_v2(
                store,
                summary,
                candidates,
                context_digest=context_digest,
                key=key,
            )
        return self._request_result("freeze-v2", value) | {
            "candidate_recipe_digest": CANDIDATE_RECIPE_DIGEST
        }

    def close_tick(
        self,
        request_digest: str,
        phase: str,
        owner_id: str,
        pid: int,
        token: str,
        repair_key: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(phase, str) or phase not in {
            "compile",
            "deliver",
            "finalize",
            "repair",
            "reconcile-delivery",
        }:
            raise ValidationFailed("Local continuity exact close phase required")
        if (phase == "repair") != (repair_key is not None):
            raise ValidationFailed("Local continuity repair key only for explicit repair phase")
        if repair_key is not None:
            logical(repair_key, "Local close repair key")
        logical(owner_id, "Local worker owner")
        logical(token, "Local worker incarnation")
        bounded_int(pid, maximum=2**31 - 1)
        digest_text(request_digest)
        with self._close() as (_, _, service):
            if phase == "compile":
                request = service.compile_once(
                    self.binding,
                    request_digest,
                    owner_id=owner_id,
                    owner_pid=pid,
                    owner_token=token,
                )
            elif phase == "deliver":
                request = service.deliver_once(
                    self.binding,
                    request_digest,
                    owner_id=owner_id,
                    owner_pid=pid,
                    owner_token=token,
                )
            elif phase == "repair":
                assert repair_key is not None
                request = service.repair_generated_candidates(
                    self.binding,
                    request_digest,
                    repair_key=repair_key,
                    owner_id=owner_id,
                    owner_pid=pid,
                    owner_token=token,
                )
            elif phase == "reconcile-delivery":
                request = service.reconcile_delivery(self.binding, request_digest)
            else:
                receipt = service.finalize(self.binding, request_digest)
                return _report(
                    phase, state="complete", request_digest=request_digest, receipt_digest=receipt
                )
            return self._request_result(phase, request)

    def doctor(self) -> dict[str, Any]:
        """Read-only evidence, not an automatic drain, repair or source re-admission."""
        frozen = self.controls.is_frozen(self.binding)
        if frozen:
            report = self.controls.inspect(self.binding)
        else:
            report = self.base.inspect(self.binding)
            entries = self.spool.read_session_entries(
                client_id=self.binding.client_id, session_id=self.binding.external_session_id
            )
            for entry in entries:
                validate_reviewed_control_entry(entry)
                if (
                    entry.session_id != self.binding.external_session_id
                    or entry.client_id != self.binding.client_id
                ):
                    raise PolicyViolation("Local doctor external session owner drift")
            with self._read() as db:
                rows = self.base._events(db, self.binding.session_id)
                persisted = tuple(row["spool_digest"] for row in rows if row["spool_digest"])
            expected = tuple(entry.entry_digest for entry in entries)
            if not entries or entries[0].internal_event_type != "session_start":
                report["issues"].append("missing-required-hook-events")
            if expected[: len(persisted)] != persisted:
                report["issues"].append("spool-persistence-chain-gap")
            elif len(expected) > len(persisted):
                report["issues"].append("unpersisted-spool-delta")
            report |= {"spool_event_count": len(entries), "persisted_spool_count": len(persisted)}
        try:
            self._check_current()
        except (ZekamError, OSError, sqlite3.Error):
            report["issues"].append("current-source-or-authority-stale")
            report["current_source_verified"] = False
        else:
            report["current_source_verified"] = True
        report["projection_state"] = "not-frozen"
        if frozen:
            with self._read() as db:
                _, row, _ = self.controls._frozen(db, self.binding)
                link = db.execute(
                    "select job_id,outbox_id from continuity_outbox_binding"
                    " where close_request_digest=? and session_id=?",
                    (row["request_digest"], self.binding.session_id),
                ).fetchone()
                request = FrozenClose(
                    row["request_digest"],
                    link[0],
                    link[1],
                    json.loads(row["input_json"]),
                    "pending",
                )
                projections = request.projections(self.binding)
            files = KnowledgeFileStore(self.arguments.home)
            try:
                exact = all(
                    files._read_optional(item.manifest.portable_ref, max_bytes=2 * 1024 * 1024)
                    == item.payload
                    for item in projections
                )
            except (ZekamError, OSError):
                exact = False
            if exact:
                report["projection_state"] = "exact"
            else:
                report["projection_state"] = "missing-or-drifted"
                report["issues"].append("generated-projection-missing-or-drifted")
        return report | {
            "state": "attention-required" if report["issues"] else "healthy",
            "verification_scope": "existing-db-spool-source-and-frozen-projections",
            "installed_client_lifecycle_proven": False,
            "native_ack": False,
            "read_only": True,
        }
