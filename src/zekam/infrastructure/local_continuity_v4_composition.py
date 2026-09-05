"""Dormant fixed composition for the reviewed Codex 0.151 SessionStart slice.

Construction only validates already-admitted v4 authority.  It does not bootstrap,
bind, activate hooks, read transcripts, call providers, or write project files.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.application.active_task_contract import AUTHORITY_REF
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.config import core_root
from zekam.application.context_compiler import compile_context_v2
from zekam.application.context_ranking import ContextRankingRequest, count_context_tokens
from zekam.application.fresh_bootstrap import MAX_CONFIG_BYTES
from zekam.application.local_continuity import ContinuityBinding, LocalContext, uuid_text
from zekam.application.local_continuity_source_plan import (
    MAX_SOURCE_BYTES,
    ContinuitySourceRecipe,
)
from zekam.application.local_continuity_v4_ingress import (
    FrozenCurrentStartupContext,
    _validate_current_context_inputs,
)
from zekam.application.local_continuity_v4_writer import CurrentSourceSnapshot
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
)
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.infrastructure import local_continuity_environment as environment_module
from zekam.infrastructure.clients.codex_macos_0151_lifecycle import (
    TrustedCodex0151ProcessManager,
    handled_failure_output,
    parse_codex_macos_0151,
)
from zekam.infrastructure.local_continuity_environment import LocalContinuityEnvironment
from zekam.infrastructure.local_continuity_source_plan import BoundedContinuitySource
from zekam.infrastructure.local_startup_composition import _BoundedProjectSource
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_startup import SQLiteStartupSourceResolver
from zekam.infrastructure.sqlite.local_continuity_v4_ingress import SQLiteCodexV4Ingress
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

_TRUSTED_CONTEXT_OWNERS: weakref.WeakSet[_CurrentV4SessionStartContext]


class _DormantV4Environment(LocalContinuityEnvironment):
    """Reuse the accepted environment guard with an explicit dormant-v4 DB gate."""

    def _admitted_config(self, binding: ContinuityBinding, sanitized: dict[str, Any]) -> str:
        before = environment_module._path_identity(self.operational_path)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.operational_path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                environment_module._path_identity(sidecar)
        connection = operational_schema._connect(self.operational_path, read_only=True)
        try:
            connection.execute("pragma query_only=on")
            connection.execute("begin")
            if operational_schema._validate_connection(connection) != 4:
                raise environment_module._reject("current dormant-v4 schema required")
            rows = connection.execute(
                "select id,task_digest,config_digest,length(cast(sanitized_json as blob))"
                " from config_revision where active=1 limit 2"
            ).fetchall()
            if len(rows) != 1:
                raise environment_module._reject("exactly one admitted active config required")
            row = rows[0]
            if row[1] != binding.task_digest or row[2] != binding.policy_digest:
                raise environment_module._reject("active admitted task or policy drift")
            uuid_text(row[0], "Config revision")
            if type(row[3]) is not int or not 0 < row[3] <= MAX_CONFIG_BYTES:
                raise environment_module._reject("admitted config byte bound exceeded")
            raw = connection.execute(
                "select sanitized_json from config_revision where id=?", (row[0],)
            ).fetchone()[0]
            if not isinstance(raw, str):
                raise environment_module._reject("admitted config must be JSON text")
            admitted = environment_module._document(raw.encode("utf-8"), json_format=True)
            if canonical_json(admitted) != canonical_json(sanitized):
                raise environment_module._reject("admitted settings payload drift")
            if digest(admitted) != binding.policy_digest:
                raise environment_module._reject("admitted settings digest drift")
            return str(row[0])
        finally:
            connection.rollback()
            connection.close()
            after = environment_module._path_identity(self.operational_path)
            if after[:-3] != before[:-3]:
                raise environment_module._reject("operational authority file replaced")

    def _validate(self, binding: ContinuityBinding) -> dict[str, Any]:
        report = super()._validate(binding)
        report.pop("evidence_digest")
        report["operational_schema_version"] = 4
        report["operational_schema_digest"] = operational_schema.V4_SCHEMA_DIGEST
        return {**report, "evidence_digest": digest(report)}


class _DormantV4ContinuityRead(SQLiteContinuityStore):
    """Read-only v3 startup semantics over an explicitly admitted dormant v4 DB."""

    def __init__(self, path: Path) -> None:
        current = operational_schema.status(path)
        if not current.schema_ok or not current.integrity_ok or current.schema_version != 4:
            raise PolicyViolation("Dormant Codex accepted v4 read semantics required")
        self.path = path
        self.source_resolver = None


class _DormantV4Operational(SQLiteOperationalStore):
    """Use the immutable v4 superset without changing the default-v3 store."""

    def __init__(self, path: Path) -> None:
        current = operational_schema.status(path)
        if not current.schema_ok or not current.integrity_ok or current.schema_version != 4:
            raise PolicyViolation("Dormant Codex accepted v4 operational semantics required")
        self._path = path
        self._local = threading.local()


@dataclass(frozen=True, slots=True)
class DormantCodex0151V4Arguments:
    database: Path
    home: Path
    source_root: Path
    binding: ContinuityBinding
    source_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(
            type(path) is not type(Path()) or not path.is_absolute()
            for path in (
                self.database,
                self.home,
                self.source_root,
            )
        ):
            raise ValidationFailed("Dormant Codex composition exact absolute paths required")
        if type(self.binding) is not ContinuityBinding:
            raise ValidationFailed("Dormant Codex composition exact binding required")
        self.binding.__post_init__()
        if (
            type(self.source_paths) is not tuple
            or not 1 <= len(self.source_paths) <= 8
            or tuple(sorted(set(self.source_paths))) != self.source_paths
            or any(type(path) is not str for path in self.source_paths)
        ):
            raise ValidationFailed("Dormant Codex composition exact source paths required")


@dataclass(frozen=True, slots=True)
class DormantHookExecution:
    stdout: bytes
    stderr: bytes
    exit_status: int
    hydrated: bool
    recovery_required: bool

    def __post_init__(self) -> None:
        if (
            type(self.stdout) is not bytes
            or type(self.stderr) is not bytes
            or type(self.exit_status) is not int
            or type(self.hydrated) is not bool
            or type(self.recovery_required) is not bool
        ):
            raise ValidationFailed("Dormant Codex exact execution result required")


class _CurrentV4SessionStartContext:
    __slots__ = (
        "__weakref__",
        "accepted_startup",
        "binding",
        "environment",
        "operational",
        "path",
        "source",
        "source_paths",
    )

    def __init__(
        self,
        path: Path,
        binding: ContinuityBinding,
        source: BoundedContinuitySource,
        source_paths: tuple[str, ...],
        environment: LocalContinuityEnvironment,
    ) -> None:
        self.path = path
        self.binding = binding
        self.source = source
        self.source_paths = source_paths
        self.environment = environment
        self.operational = _DormantV4Operational(path)
        plan = source.assert_snapshot(self.operational, binding.source_snapshot_id)
        self.accepted_startup = SQLiteStartupSourceResolver(
            _DormantV4ContinuityRead(path),
            _BoundedProjectSource(source, plan, binding),
            environment=environment,
        )
        _TRUSTED_CONTEXT_OWNERS.add(self)

    def _rows(self) -> dict[str, Any]:
        environment_report = self.environment.validate(self.binding)
        accepted_rows = self.accepted_startup._rows(self.binding)
        plan = self.source.assert_snapshot(self.operational, self.binding.source_snapshot_id)
        self.source.probe(self.operational, self.binding)
        if plan.recipe.allowed_paths != self.source_paths:
            raise PolicyViolation("Dormant Codex source recipe path drift")
        with sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("pragma query_only=on")
            db.execute("begin")
            binding_row = db.execute(
                "select b.*,s.status from continuity_session_binding b "
                "join session s on s.id=b.session_id where b.session_id=?",
                (self.binding.session_id,),
            ).fetchone()
            config = db.execute(
                "select * from config_revision where active=1 and config_digest=? "
                "and task_digest=?",
                (self.binding.policy_digest, self.binding.task_digest),
            ).fetchone()
            work = (
                None
                if self.binding.work_item_id is None
                else db.execute(
                    "select w.id as work_id,w.project_id,w.kind,w.title,w.state as current_state,"
                    "w.revision as current_revision,w.evidence_digest as current_evidence,r.* "
                    "from work_item w join work_revision r on r.work_item_id=w.id "
                    "and r.revision=w.revision where w.id=? and w.project_id=?",
                    (self.binding.work_item_id, self.binding.project_id),
                ).fetchone()
            )
            run = (
                None
                if self.binding.run_id is None
                else db.execute("select * from run where id=?", (self.binding.run_id,)).fetchone()
            )
            snapshot = db.execute(
                "select * from source_snapshot where id=?",
                (self.binding.source_snapshot_id,),
            ).fetchone()
        if (
            binding_row is None
            or any(
                binding_row[key] != value
                for key, value in {
                    "session_id": self.binding.session_id,
                    "external_session_id": self.binding.external_session_id,
                    "project_id": self.binding.project_id,
                    "realm_id": self.binding.realm_id,
                    "work_item_id": self.binding.work_item_id,
                    "run_id": self.binding.run_id,
                    "client_id": self.binding.client_id,
                    "device_id": self.binding.device_id,
                    "source_snapshot_id": self.binding.source_snapshot_id,
                    "task_digest": self.binding.task_digest,
                    "plan_digest": self.binding.plan_digest,
                    "policy_digest": self.binding.policy_digest,
                    "binding_digest": self.binding.binding_digest,
                }.items()
            )
            or binding_row["status"] != "open"
            or config is None
            or work is None
            or run is None
            or snapshot is None
            or snapshot["revision_ref"] != plan.revision_ref
            or work["work_id"] != self.binding.work_item_id
            or work["project_id"] != self.binding.project_id
            or run["work_item_id"] != self.binding.work_item_id
            or run["config_revision_id"] != config["id"]
            or run["source_snapshot_id"] != self.binding.source_snapshot_id
            or run["plan_digest"] != self.binding.plan_digest
        ):
            raise PolicyViolation("Dormant Codex current startup authority unavailable")
        try:
            config_body = json.loads(config["sanitized_json"])
            work_payload = json.loads(work["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PolicyViolation("Dormant Codex current startup rows malformed") from exc
        if (
            type(config_body) is not dict
            or canonical_json(config_body) != config["sanitized_json"]
            or digest(config_body) != self.binding.policy_digest
            or type(work_payload) is not dict
            or canonical_json(work_payload) != work["payload_json"]
            or digest(work_payload) != work["payload_digest"]
            or work["state"] != work["current_state"]
            or run["status"] == "unknown"
        ):
            raise PolicyViolation("Dormant Codex current startup row parity drift")
        return {
            "plan": plan,
            "config": dict(config),
            "config_body": config_body,
            "work": dict(work),
            "work_payload": work_payload,
            "run": dict(run),
            "snapshot": dict(snapshot),
            "environment": environment_report,
            "accepted_startup": accepted_rows,
        }

    def _context(self, observed_at: str, rows: dict[str, Any]) -> LocalContext:
        observed = dt.datetime.fromisoformat(observed_at)
        accepted = rows["accepted_startup"]
        values = (
            (
                "startup-system-policy",
                ContextCandidateKind.SYSTEM_POLICY,
                *accepted["system-policy"],
            ),
            (
                "startup-work-contract",
                ContextCandidateKind.WORK_CONTRACT,
                *accepted["work-contract"],
            ),
            (
                "startup-run-status",
                ContextCandidateKind.RUN_STATUS,
                *accepted["run-status"],
            ),
        )
        candidates: list[ContextCandidate] = []
        fragments: list[tuple[str, str]] = []
        for identifier, kind, text, revision, ref, scope, canonical_id in values:
            candidate = ContextCandidate(
                candidate_id=identifier,
                authority=AuthorityLevel.CANONICAL,
                observed_at=observed,
                source_revision=revision,
                content_digest=digest(text),
                token_count=count_context_tokens(text),
                required=True,
                kind=kind,
                source_ref=ref,
                scope_ref=scope,
                identity_refs=(f"work/{self.binding.work_item_id}",),
                applicable_roles=("builder",),
                canonical_revision_id=canonical_id,
            )
            candidates.append(candidate)
            fragments.append((identifier, text))
        plan = rows["plan"]
        for pinned in plan.files:
            payload = self.source._read(pinned.relative_path, MAX_SOURCE_BYTES)
            if (
                payload is None
                or len(payload) != pinned.size_bytes
                or digest_of_bytes(payload) != pinned.content_digest
            ):
                raise PolicyViolation("Dormant Codex bounded source bytes drift")
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PolicyViolation("Dormant Codex bounded source UTF-8 required") from exc
            identifier = f"startup-source-{digest(pinned.relative_path)[7:23]}"
            candidate = ContextCandidate(
                candidate_id=identifier,
                authority=AuthorityLevel.VERIFIED,
                observed_at=observed,
                source_revision=plan.revision_ref,
                content_digest=digest(text),
                token_count=count_context_tokens(text),
                required=True,
                kind=ContextCandidateKind.SOURCE_SLICE,
                source_ref=pinned.relative_path,
                scope_ref=f"project/{self.binding.project_id}",
                identity_refs=(f"work/{self.binding.work_item_id}",),
                applicable_roles=("builder",),
                canonical_revision_id=self.binding.source_snapshot_id,
            )
            candidates.append(candidate)
            fragments.append((identifier, text))
        ranking = ContextRankingRequest(
            role="builder",
            target_identity_refs=(f"work/{self.binding.work_item_id}",),
            step_scope_ref=None,
            work_scope_ref=f"work/{self.binding.work_item_id}",
            project_scope_ref=f"project/{self.binding.project_id}",
            realm_scope_ref=f"realm/{self.binding.realm_id}",
            current_source_revision=plan.revision_ref,
            compatible_source_revisions=tuple(
                sorted({candidate.source_revision for candidate in candidates})
            ),
            task_terms=(),
            tokenizer_profile_digest=DEFAULT_TOKENIZER_PROFILE_DIGEST,
        )
        compiled = compile_context_v2(
            tuple(candidates),
            ranking_request=ranking,
            token_budget=2048,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=observed,
            recipe_id="local-startup-required-v1",
            recipe_digest=digest("local-startup-required-v1"),
            target_role="builder",
            contents=dict(fragments),
            ranking_snapshot_digest=digest(ranking.body()),
            candidate_set_digest=digest([candidate.candidate_digest for candidate in candidates]),
        )
        selected = tuple(item.candidate_id for item in compiled.selected)
        if {candidate.candidate_id for candidate in candidates} != set(selected):
            raise PolicyViolation("Dormant Codex required startup fragments omitted")
        fragment_by_id = dict(fragments)
        candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
        return LocalContext(
            compiled,
            tuple((identifier, fragment_by_id[identifier]) for identifier in selected),
            ranking,
            tuple(candidate_by_id[identifier] for identifier in selected),
        )

    def build(
        self,
        binding: ContinuityBinding,
        *,
        hydration_key: str,
        observed_at: str,
    ) -> FrozenCurrentStartupContext:
        if not _trusted_context_owner(self):
            raise PolicyViolation("Dormant Codex unsealed context owner")
        if binding != self.binding:
            raise PolicyViolation("Dormant Codex context binding drift")
        rows = self._rows()
        plan = rows["plan"]
        context = self._context(observed_at, rows)
        source_snapshot = CurrentSourceSnapshot(
            binding.source_snapshot_id, plan.revision_ref, plan.content_digest
        )
        environment_evidence_digest = digest(
            {
                "environment_evidence_digest": rows["environment"]["evidence_digest"],
                "source_plan_digest": plan.content_digest,
                "binding_digest": binding.binding_digest,
            }
        )
        manifest_body, hydration_body, additional, success_stdout = (
            _validate_current_context_inputs(
                binding=binding,
                context=context,
                source_snapshot=source_snapshot,
                environment_evidence_digest=environment_evidence_digest,
                hydration_key=hydration_key,
                observed_at=observed_at,
            )
        )
        frozen = object.__new__(FrozenCurrentStartupContext)
        object.__setattr__(frozen, "binding", binding)
        object.__setattr__(frozen, "binding_digest", binding.binding_digest)
        object.__setattr__(frozen, "source_snapshot", source_snapshot)
        object.__setattr__(frozen, "environment_evidence_digest", environment_evidence_digest)
        object.__setattr__(frozen, "context", context)
        object.__setattr__(frozen, "manifest_body_json", canonical_json(manifest_body))
        object.__setattr__(frozen, "manifest_digest", digest(manifest_body))
        object.__setattr__(frozen, "hydration_key", hydration_key)
        object.__setattr__(frozen, "hydration_body_json", canonical_json(hydration_body))
        object.__setattr__(frozen, "hydration_receipt_digest", digest(hydration_body))
        object.__setattr__(frozen, "observed_at", observed_at)
        object.__setattr__(frozen, "additional_context", additional)
        object.__setattr__(frozen, "output_digest", digest(additional))
        object.__setattr__(frozen, "success_stdout", success_stdout)
        frozen.__post_init__()
        return frozen

    def assert_current(
        self, binding: ContinuityBinding, snapshot: FrozenCurrentStartupContext
    ) -> None:
        if not _trusted_context_owner(self):
            raise PolicyViolation("Dormant Codex unsealed context owner")
        rebuilt = self.build(
            binding,
            hydration_key=snapshot.hydration_key,
            observed_at=snapshot.observed_at,
        )
        if rebuilt != snapshot:
            raise PolicyViolation("Dormant Codex current context changed")


_TRUSTED_CONTEXT_OWNERS = weakref.WeakSet()


def _trusted_context_owner(value: object) -> bool:
    return type(value) is _CurrentV4SessionStartContext and value in _TRUSTED_CONTEXT_OWNERS


class DormantCodex0151V4Runtime:
    def __init__(self, arguments: DormantCodex0151V4Arguments) -> None:
        if type(arguments) is not DormantCodex0151V4Arguments:
            raise ValidationFailed("Dormant Codex exact arguments required")
        arguments.__post_init__()
        current = operational_schema.status(arguments.database)
        if not current.schema_ok or not current.integrity_ok or current.schema_version != 4:
            raise PolicyViolation("Dormant Codex composition requires accepted schema v4")
        with sqlite3.connect(arguments.database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute(
                "select ss.source_binding_id from continuity_session_binding b "
                "join source_snapshot ss on ss.id=b.source_snapshot_id "
                "where b.session_id=? and b.binding_digest=?",
                (arguments.binding.session_id, arguments.binding.binding_digest),
            ).fetchone()
        if row is None:
            raise PolicyViolation("Dormant Codex existing binding/source required")
        recipe = ContinuitySourceRecipe(
            arguments.binding.project_id,
            arguments.binding.realm_id,
            str(row[0]),
            arguments.source_paths,
            arguments.binding.task_digest,
            arguments.binding.policy_digest,
        )
        source = BoundedContinuitySource(arguments.source_root, recipe)
        source.assert_snapshot(
            _DormantV4Operational(arguments.database), arguments.binding.source_snapshot_id
        )
        authority_root = core_root()
        environment = _DormantV4Environment(
            arguments.home,
            authority_root,
            authority_root / AUTHORITY_REF,
            arguments.database,
        )
        environment.validate(arguments.binding)
        manager = TrustedCodex0151ProcessManager()
        spool = ClientLifecycleSpool(arguments.home, client_id="codex")
        context = _CurrentV4SessionStartContext(
            arguments.database,
            arguments.binding,
            source,
            arguments.source_paths,
            environment,
        )
        self.arguments = arguments
        self.manager = manager
        self.spool = spool
        self.ingress = SQLiteCodexV4Ingress(
            arguments.database,
            arguments.binding,
            process_manager=manager,
            context_port=context,
            spool=spool,
        )

    def handle(self, payload: bytes) -> DormantHookExecution:
        try:
            event = parse_codex_macos_0151(payload, expected_root=self.arguments.source_root)
            if event.event_type != "SessionStart":
                raise PolicyViolation("Dormant Codex slice accepts SessionStart only")
            self.ingress.attach_process()
            result = self.ingress.session_start(event)
            return DormantHookExecution(
                result.stdout,
                b"",
                0,
                result.manifest_digest is not None,
                result.recovery_required,
            )
        except (ValidationFailed, PolicyViolation, ConcurrencyConflict, ConfigurationError):
            return DormantHookExecution(
                handled_failure_output(recovery_required=False), b"", 0, False, False
            )
