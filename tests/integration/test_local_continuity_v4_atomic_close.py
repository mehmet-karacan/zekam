"""Process-only integration gates for the dormant operational-v4 close writer."""

from __future__ import annotations

import contextlib
import datetime as dt
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zekam.application.context_compiler import compile_context_v2
from zekam.application.context_ranking import ContextRankingRequest, count_context_tokens
from zekam.application.local_continuity import (
    ContinuityBinding,
    ContinuityEvent,
    ContinuityTail,
    LocalContext,
)
from zekam.application.local_continuity_close import (
    CloseCandidateBundle,
    CloseSummary,
    FrozenClose,
)
from zekam.application.local_continuity_v4_writer import (
    CanonicalManifestProvenance,
    CurrentSourceSnapshot,
    ExactResolvedRecovery,
    FinalizeClosedWriteRequest,
    FrozenCloseWriteRequest,
    FrozenProjectionSnapshot,
    FrozenSpoolSnapshot,
    ResolvedManifestFragment,
    revision_digest,
)
from zekam.application.local_hook_command_contract import (
    NATIVE_DOUBLE_EXEC_TOPOLOGY,
    ReviewedHookCommand,
)
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
)
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7
from zekam.infrastructure.sqlite import local_continuity as continuity_module
from zekam.infrastructure.sqlite import local_runtime as runtime_module
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_v4_writer import (
    SQLiteDormantV4CloseWriter,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

pytestmark = pytest.mark.integration

NOW = "2026-09-03T12:00:00+00:00"
SESSION_ID = "018f0000-0000-7000-8000-000000000001"
PROJECT_ID = "018f0000-0000-7000-8000-000000000002"
REALM_ID = "018f0000-0000-7000-8000-000000000003"
SNAPSHOT_ID = "018f0000-0000-7000-8000-000000000004"
ATTACHMENT_ID = "018f0000-0000-7000-8000-000000000005"
DELIVERY_ID = "018f0000-0000-7000-8000-000000000006"
SOURCE_REF = "src/akilli_kasa/api/saglik.py"


def _revision_body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "attachment_id": ATTACHMENT_ID,
        "revision_number": 1,
        "previous_revision_digest": None,
        "operation_key": "attach",
        "state": "attached",
        "process_generation_digest": digest("generation"),
        "active_manifest_digest": None,
        "active_hydration_receipt_digest": None,
        "checkpoint_digest": None,
        "pre_compaction_event_digest": None,
        "post_compaction_event_digest": None,
        "close_request_digest": None,
        "pre_close_event_digest": None,
        "close_receipt_digest": None,
        "session_closed_event_digest": None,
        "hook_recovery_case_id": None,
        "hook_recovery_resolution_id": None,
        "local_recovery_case_id": None,
        "local_recovery_resolution_id": None,
        "crash_recovered_event_digest": None,
        "crash_recovered_receipt_digest": None,
        "created_at": NOW,
    }
    body.update(changes)
    return body


def _insert_revision(db: sqlite3.Connection, **changes: object) -> str:
    body = _revision_body(**changes)
    value = revision_digest(body)
    stored = {"revision_digest": value, **body}
    columns = tuple(stored)
    db.execute(
        "insert into continuity_hook_attachment_revision("
        + ",".join(columns)
        + ",body_json) values("
        + ",".join("?" for _ in range(len(columns) + 1))
        + ")",
        (*stored.values(), canonical_json(stored)),
    )
    return value


def _seed(path: Path, *, capacity: int = 64) -> dict[str, object]:
    operational_schema.bootstrap_v4(path)
    binding = ContinuityBinding(
        SESSION_ID,
        "external-session",
        PROJECT_ID,
        REALM_ID,
        "codex",
        "macbook",
        SNAPSHOT_ID,
        digest("task"),
        digest("plan"),
        digest("policy"),
    )
    source_text = (Path("/Users/mkaracan/Projeler/akilli-kasa") / SOURCE_REF).read_text()
    source_digest = digest(source_text)
    candidate = ContextCandidate(
        candidate_id="health-source",
        authority=AuthorityLevel.VERIFIED,
        observed_at=dt.datetime.fromisoformat(NOW),
        source_revision="HEAD",
        content_digest=source_digest,
        token_count=count_context_tokens(source_text),
        required=True,
        kind=ContextCandidateKind.SOURCE_SLICE,
        source_ref=SOURCE_REF,
        identity_refs=("task/wp08-v4",),
        scope_ref=f"project/{PROJECT_ID}",
        applicable_roles=("builder",),
    )
    ranking = ContextRankingRequest(
        role="builder",
        target_identity_refs=("task/wp08-v4",),
        step_scope_ref=None,
        work_scope_ref=None,
        project_scope_ref=f"project/{PROJECT_ID}",
        realm_scope_ref=f"realm/{REALM_ID}",
        current_source_revision="HEAD",
        compatible_source_revisions=(),
        task_terms=(),
        tokenizer_profile_digest=DEFAULT_TOKENIZER_PROFILE_DIGEST,
    )
    compiled = compile_context_v2(
        (candidate,),
        ranking_request=ranking,
        token_budget=4096,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=dt.datetime.fromisoformat(NOW),
        contents={"health-source": source_text},
        ranking_snapshot_digest=digest(ranking.body()),
        candidate_set_digest=digest(candidate.candidate_digest),
        recipe_id="continuity-v2",
        recipe_digest=digest("recipe"),
        target_role="builder",
    )
    context = LocalContext(
        compiled,
        (("health-source", source_text),),
        ranking,
        (candidate,),
    )
    manifest_body = {
        "binding_digest": binding.binding_digest,
        "session_id": SESSION_ID,
        "checkpoint_digest": None,
        "context": context.body(),
    }
    manifest_digest = digest(manifest_body)
    hydration_digest = digest(
        {
            "session_id": SESSION_ID,
            "manifest_digest": manifest_digest,
            "idempotency_key": "hydrate",
            "grants_authority": False,
        }
    )
    attachment_body = {
        "attachment_id": ATTACHMENT_ID,
        "client_contract_digest": digest("client-contract"),
        "created_at": NOW,
        "hook_set_digest": digest("hook-set"),
        "native_artifact_digest": digest("native-artifact"),
        "session_id": SESSION_ID,
    }
    managed_body = {
        "ancestry_policy_digest": digest("ancestry-policy"),
        "attachment_id": ATTACHMENT_ID,
        "created_at": NOW,
        "hook_set_digest": digest("hook-set"),
        "native_artifact_digest": digest("native-artifact"),
        "native_pid": 101,
        "native_start_token": "native-start",
        "native_uid": 501,
        "predecessor_process_generation_digest": None,
        "transition_kind": "initial-attach",
    }
    managed_digest = digest("managed-receipt")
    generation_body = {
        "ancestry_policy_digest": digest("ancestry-policy"),
        "attachment_id": ATTACHMENT_ID,
        "created_at": NOW,
        "generation": 1,
        "hook_set_digest": digest("hook-set"),
        "managed_launch_receipt_digest": managed_digest,
        "native_artifact_digest": digest("native-artifact"),
        "native_pid": 101,
        "native_start_token": "native-start",
        "native_uid": 501,
        "previous_process_generation_digest": None,
    }
    generation_digest = digest("generation")
    commands = tuple(
        ReviewedHookCommand(
            attachment_id=ATTACHMENT_ID,
            external_event_type=event_type,
            topology=NATIVE_DOUBLE_EXEC_TOPOLOGY,
            client_contract_digest=digest("client-contract"),
            hook_set_digest=digest("hook-set"),
            shell_artifact_digest=digest("shell"),
            python_launcher_artifact_digest=digest("launcher"),
            python_runtime_artifact_digest=digest("runtime"),
            argv_recipe_digest=digest(f"argv:{event_type}"),
            sandbox_profile_digest=digest("sandbox"),
            created_at=NOW,
        )
        for event_type in ("SessionStart", "PreCompact", "PostCompact")
    )
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into project(id,slug,display_name,created_at) values(?,?,?,?)",
            (PROJECT_ID, "akilli-kasa", "Akilli Kasa", NOW),
        )
        db.execute("insert into project_knowledge_realm values(?,?,?)", (PROJECT_ID, REALM_ID, NOW))
        db.execute(
            "insert into source_binding values(?,?,?,?,?,?)",
            ("source", PROJECT_ID, "source:akilli-kasa", "directory", 1, NOW),
        )
        db.execute(
            "insert into source_snapshot values(?,?,?,?,?,?,?)",
            (SNAPSHOT_ID, "source", "HEAD", digest("tree"), source_digest, digest("config"), NOW),
        )
        db.execute(
            "insert into session(id,client_id,device_id,project_id,status,opened_at) "
            "values(?,?,?,?,?,?)",
            (SESSION_ID, "codex", "macbook", PROJECT_ID, "open", NOW),
        )
        db.execute("insert into local_runtime_config values(1,?)", (capacity,))
        db.execute(
            "insert into continuity_session_binding values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                SESSION_ID,
                "external-session",
                PROJECT_ID,
                REALM_ID,
                None,
                None,
                "codex",
                "macbook",
                SNAPSHOT_ID,
                digest("task"),
                digest("plan"),
                digest("policy"),
                binding.binding_digest,
                NOW,
            ),
        )
        db.execute(
            "insert into continuity_hook_attachment values(?,?,?,?,?,?,?,?)",
            (
                ATTACHMENT_ID,
                SESSION_ID,
                digest("client-contract"),
                digest("native-artifact"),
                digest("hook-set"),
                digest("attachment"),
                canonical_json(attachment_body),
                NOW,
            ),
        )
        db.execute(
            "insert into continuity_managed_process_receipt values(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                managed_digest,
                ATTACHMENT_ID,
                None,
                101,
                501,
                "native-start",
                digest("native-artifact"),
                digest("hook-set"),
                digest("ancestry-policy"),
                "initial-attach",
                canonical_json(managed_body),
                NOW,
            ),
        )
        db.execute(
            "insert into continuity_hook_process_generation values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                generation_digest,
                ATTACHMENT_ID,
                1,
                101,
                501,
                "native-start",
                digest("native-artifact"),
                digest("hook-set"),
                digest("ancestry-policy"),
                None,
                managed_digest,
                canonical_json(generation_body),
                NOW,
            ),
        )
        attached = _insert_revision(db, process_generation_digest=generation_digest)
        for command in commands:
            db.execute(
                "insert into continuity_reviewed_hook_command "
                "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    command.command_digest,
                    command.attachment_id,
                    command.external_event_type,
                    command.topology,
                    command.client_contract_digest,
                    command.hook_set_digest,
                    command.shell_artifact_digest,
                    command.python_launcher_artifact_digest,
                    command.python_runtime_artifact_digest,
                    command.argv_recipe_digest,
                    command.sandbox_profile_digest,
                    canonical_json(command.body()),
                    command.created_at,
                    0,
                    0,
                ),
            )
        db.execute(
            "insert into context_manifest values(?,?,?,?,?,?,?)",
            (
                manifest_digest,
                SESSION_ID,
                None,
                compiled.token_budget,
                sum(item.token_count for item in compiled.selected),
                canonical_json(manifest_body),
                NOW,
            ),
        )
        db.execute(
            "insert into hydration_receipt values(?,?,?,?,?)",
            (hydration_digest, SESSION_ID, manifest_digest, "hydrate", NOW),
        )
        ancestry = {
            "receipt_digest": digest("ancestry"),
            "process_generation_digest": generation_digest,
            "delivery_id": DELIVERY_ID,
            "topology": NATIVE_DOUBLE_EXEC_TOPOLOGY,
            "launch_command_digest": commands[0].command_digest,
            "external_event_type": "SessionStart",
            "ancestry_policy_digest": digest("ancestry-policy"),
            "native_pid": 101,
            "native_start_token": "native-start",
            "native_uid": 501,
            "native_artifact_digest": digest("native-artifact"),
            "shell_pid": 202,
            "shell_start_token": "shell-start",
            "shell_uid": 501,
            "shell_parent_pid": 101,
            "shell_parent_start_token": "native-start",
            "shell_parent_uid": 501,
            "shell_artifact_digest": digest("shell"),
            "hook_pid": 202,
            "hook_start_token": "shell-start",
            "hook_uid": 501,
            "hook_parent_pid": 101,
            "hook_parent_start_token": "native-start",
            "hook_parent_uid": 501,
            "python_launcher_artifact_digest": digest("launcher"),
            "python_runtime_artifact_digest": digest("runtime"),
            "observation_digest": digest("observation"),
            "observed_at": NOW,
            "grants_authority": 0,
            "approval_inherited": 0,
        }
        ancestry_body = {
            "schema": "zekam-hook-invocation-ancestry-receipt/v1",
            **{key: value for key, value in ancestry.items() if key != "receipt_digest"},
        }
        ancestry["receipt_digest"] = digest(ancestry_body)
        columns = tuple(ancestry)
        db.execute(
            "insert into continuity_hook_invocation_ancestry_receipt("
            + ",".join(columns)
            + ",body_json) values("
            + ",".join("?" for _ in range(len(columns) + 1))
            + ")",
            (*ancestry.values(), canonical_json(ancestry_body)),
        )
        spool_digest = digest("session-start-spool")
        event = ContinuityEvent("SESSION_START", "session-start", NOW, spool_digest=spool_digest)
        envelope = {
            "session_id": SESSION_ID,
            "binding_digest": binding.binding_digest,
            "sequence": 1,
            "previous_digest": None,
            "event": event.body(),
        }
        event_value = digest(envelope)
        native = {
            "receipt_digest": digest("pending-native-receipt"),
            "event_digest": event_value,
            "attachment_revision_digest": attached,
            "process_generation_digest": generation_digest,
            "ancestry_receipt_digest": ancestry["receipt_digest"],
            "spool_digest": spool_digest,
            "previous_spool_digest": None,
            "observation_digest": ancestry["observation_digest"],
            "delivery_id": DELIVERY_ID,
            "spool_sequence": 1,
            "external_event_type": "SessionStart",
            "internal_event_type": "SESSION_START",
            "external_turn_id": None,
            "external_trigger_id": None,
            "shell_pid": 202,
            "shell_uid": 501,
            "shell_start_token": "shell-start",
            "hook_pid": 202,
            "hook_uid": 501,
            "hook_start_token": "shell-start",
            "shell_artifact_digest": digest("shell"),
            "python_launcher_artifact_digest": digest("launcher"),
            "python_runtime_artifact_digest": digest("runtime"),
            "hydration_receipt_digest": hydration_digest,
            "grants_authority": 0,
            "approval_inherited": 0,
            "created_at": NOW,
        }
        native_body = {key: value for key, value in native.items() if key != "receipt_digest"}
        native["receipt_digest"] = digest(native_body)
        native_columns = tuple(native)
        db.execute(
            "insert into continuity_native_event_receipt("
            + ",".join(native_columns)
            + ",body_json) values("
            + ",".join("?" for _ in range(len(native_columns) + 1))
            + ")",
            (
                *native.values(),
                canonical_json(native_body),
            ),
        )
        db.execute(
            "insert into session_event values(?,?,?,?,?)",
            (str(DELIVERY_ID), SESSION_ID, "SESSION_START", event_value, NOW),
        )
        db.execute(
            "insert into session_event_detail values(?,?,?,?,?,?,?,?)",
            (
                str(DELIVERY_ID),
                SESSION_ID,
                1,
                None,
                "session-start",
                event_value,
                spool_digest,
                canonical_json(envelope),
            ),
        )
        hydrated = _insert_revision(
            db,
            revision_number=2,
            previous_revision_digest=attached,
            operation_key="hydrate",
            state="hydrated",
            process_generation_digest=generation_digest,
            active_manifest_digest=manifest_digest,
            active_hydration_receipt_digest=hydration_digest,
        )
        db.commit()
    return {
        "binding": binding,
        "source_digest": source_digest,
        "manifest_digest": manifest_digest,
        "generation_digest": generation_digest,
        "revision_digest": hydrated,
        "tail": ContinuityTail(1, event_value),
        "spool_digest": spool_digest,
    }


class _Source:
    def __init__(self) -> None:
        self.fail = False
        self.checks = 0
        self.resolves = 0
        self.resolve_mode = "valid"
        self.snapshot_mode = "valid"

    def snapshot(self, binding: ContinuityBinding) -> CurrentSourceSnapshot:
        assert binding.session_id == SESSION_ID
        if self.snapshot_mode == "wrong-type":
            return object()  # type: ignore[return-value]
        if self.snapshot_mode == "wrong-id":
            return CurrentSourceSnapshot("different-snapshot", "HEAD", digest("current-source"))
        if self.snapshot_mode == "wrong-revision":
            return CurrentSourceSnapshot(SNAPSHOT_ID, "STALE", digest("current-source"))
        return CurrentSourceSnapshot(SNAPSHOT_ID, "HEAD", digest("current-source"))

    def resolve_fragment(
        self,
        binding: ContinuityBinding,
        snapshot: CurrentSourceSnapshot,
        provenance: CanonicalManifestProvenance,
    ) -> ResolvedManifestFragment:
        self.resolves += 1
        assert binding.session_id == SESSION_ID
        assert snapshot.source_snapshot_id == SNAPSHOT_ID
        if self.resolve_mode == "exception":
            raise PolicyViolation("injected resolver timeout")
        if self.resolve_mode == "os-error":
            raise OSError("injected local source error")
        if self.resolve_mode == "timeout":
            raise TimeoutError("injected local source timeout")
        if self.resolve_mode == "wrong-type":
            return object()  # type: ignore[return-value]
        if self.resolve_mode == "wrong-id":
            return ResolvedManifestFragment("different-candidate", "different")
        if self.resolve_mode == "wrong-bytes":
            return ResolvedManifestFragment(provenance.candidate_id, "different")
        return ResolvedManifestFragment(
            provenance.candidate_id,
            (Path("/Users/mkaracan/Projeler/akilli-kasa") / SOURCE_REF).read_text(),
        )

    def assert_current(self, binding: ContinuityBinding, snapshot: CurrentSourceSnapshot) -> None:
        self.checks += 1
        if (
            self.fail
            or snapshot.source_snapshot_id != SNAPSHOT_ID
            or snapshot.revision_ref != "HEAD"
            or snapshot.snapshot_digest != digest("current-source")
        ):
            raise PolicyViolation("injected source drift")


class _SpoolHandle:
    def __init__(self, snapshot: FrozenSpoolSnapshot) -> None:
        self.snapshot = snapshot
        self.fail = False
        self.checks = 0

    def recheck(self) -> None:
        self.checks += 1
        if self.fail:
            raise PolicyViolation("injected spool drift")


class _Spool:
    def __init__(self, snapshot: FrozenSpoolSnapshot) -> None:
        self.handle = _SpoolHandle(snapshot)

    @contextlib.contextmanager
    def frozen(self, binding: ContinuityBinding) -> Iterator[_SpoolHandle]:
        assert binding.session_id == SESSION_ID
        yield self.handle


class _Projections:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.fail_after = fail_after

    @contextlib.contextmanager
    def frozen(self, request: FrozenClose) -> Iterator[_ProjectionHandle]:
        evidence = tuple(
            sorted(
                (item.evidence() for item in request.projections(_binding())),
                key=lambda item: item["portable_ref"],
            )
        )
        yield _ProjectionHandle(FrozenProjectionSnapshot(evidence), fail_after=self.fail_after)


class _ProjectionHandle:
    def __init__(
        self, snapshot: FrozenProjectionSnapshot, *, fail_after: int | None = None
    ) -> None:
        self.snapshot = snapshot
        self.fail_after = fail_after
        self.checks = 0

    def recheck(self) -> None:
        self.checks += 1
        if self.fail_after is not None and self.checks >= self.fail_after:
            raise PolicyViolation("injected projection drift")


def _binding() -> ContinuityBinding:
    return ContinuityBinding(
        SESSION_ID,
        "external-session",
        PROJECT_ID,
        REALM_ID,
        "codex",
        "macbook",
        SNAPSHOT_ID,
        digest("task"),
        digest("plan"),
        digest("policy"),
    )


def _unsafe_fixture_mutation(
    path: Path,
    *,
    trigger_names: tuple[str, ...],
    statement: str,
    parameters: tuple[object, ...] = (),
) -> None:
    """Inject corruption while restoring the exact production schema fingerprint."""

    with sqlite3.connect(path) as db:
        trigger_sql = []
        for name in trigger_names:
            row = db.execute(
                "select sql from sqlite_master where type='trigger' and name=?", (name,)
            ).fetchone()
            assert row is not None
            trigger_sql.append(str(row[0]))
            db.execute(f'drop trigger "{name}"')
        db.execute(statement, parameters)
        for sql in trigger_sql:
            db.execute(sql)
        db.commit()
    assert operational_schema.status(path).schema_ok


def _request(
    seed: dict[str, object], *, candidates: CloseCandidateBundle | None = None
) -> FrozenCloseWriteRequest:
    summary = CloseSummary(
        ("Verified the bounded Akilli Kasa health source.",),
        (),
        (),
        ("Continue independent dormant-v4 verification.",),
        "Await independent review.",
        ((SOURCE_REF, str(seed["source_digest"])),),
        ((f"context/{str(seed['manifest_digest'])[7:]}", str(seed["manifest_digest"])),),
    )
    return FrozenCloseWriteRequest(
        _binding(),
        str(seed["revision_digest"]),
        str(seed["generation_digest"]),
        seed["tail"],  # type: ignore[arg-type]
        str(seed["manifest_digest"]),
        "close-checkpoint",
        "close",
        summary,
        candidates,
        NOW,
    )


def _v4_writer(
    path: Path, seed: dict[str, object], *, source: _Source | None = None
) -> SQLiteDormantV4CloseWriter:
    return SQLiteDormantV4CloseWriter(
        path,
        source=_Source() if source is None else source,
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )


def _materialize_and_complete(
    path: Path, frozen: FrozenClose, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=30,
        now=NOW,
    )
    assert work is not None
    evidence = frozen.compile_evidence(_binding())
    claim, created = runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=evidence,
        idempotency_key=frozen.effect_key,
        now=NOW,
    )
    assert created
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into continuity_effect_binding values(?,?,?,?)",
            (claim.id, SESSION_ID, frozen.job_id, _binding().binding_digest),
        )
        for item in frozen.projections(_binding()):
            manifest = item.manifest
            db.execute(
                "insert into knowledge_note values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(new_uuid7()),
                    REALM_ID,
                    PROJECT_ID,
                    "akilli-kasa",
                    manifest.owner_scope,
                    manifest.portable_ref,
                    manifest.note_kind,
                    manifest.authorship,
                    manifest.classification.value,
                    manifest.content_digest,
                    1,
                    digest_of_bytes(item.payload),
                    manifest.state,
                    None,
                    NOW,
                    NOW,
                ),
            )
        db.commit()
    runtime.record_receipt(claim, status="completed", evidence_digest=evidence, now=NOW)
    runtime.finish(work, state="completed", evidence_digest=evidence, now=NOW)
    with sqlite3.connect(path) as db:
        outboxes = db.execute(
            "select id,event_kind from local_outbox where job_id=? order by id",
            (frozen.job_id,),
        ).fetchall()
    assert len(outboxes) == 3
    for outbox_id, kind in outboxes:
        claimed = runtime.claim_outbox(
            supported_kinds=(str(kind),),
            outbox_id=str(outbox_id),
            require_completed_job=True,
            owner_id="delivery",
            owner_pid=102,
            owner_token="delivery-incarnation",
            lease_seconds=30,
            now=NOW,
        )
        assert claimed is not None
        delivery_evidence = (
            frozen.delivery_evidence(_binding())
            if outbox_id == frozen.outbox_id
            else digest({"outbox_id": outbox_id, "delivered": True})
        )
        runtime.record_outbox_receipt(
            claimed, status="delivered", evidence_digest=delivery_evidence, now=NOW
        )


def _materialize_runtime_variant(
    path: Path,
    frozen: FrozenClose,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    resolved_unknown_delivery: bool,
) -> None:
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=30,
        now=NOW,
    )
    assert work is not None
    evidence = frozen.compile_evidence(_binding())
    claim, created = runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=evidence,
        idempotency_key=frozen.effect_key,
        now=NOW,
    )
    assert created
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into continuity_effect_binding values(?,?,?,?)",
            (claim.id, SESSION_ID, frozen.job_id, _binding().binding_digest),
        )
        for item in frozen.projections(_binding()):
            manifest = item.manifest
            db.execute(
                "insert into knowledge_note values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(new_uuid7()),
                    REALM_ID,
                    PROJECT_ID,
                    "akilli-kasa",
                    manifest.owner_scope,
                    manifest.portable_ref,
                    manifest.note_kind,
                    manifest.authorship,
                    manifest.classification.value,
                    manifest.content_digest,
                    1,
                    digest_of_bytes(item.payload),
                    manifest.state,
                    None,
                    NOW,
                    NOW,
                ),
            )
        db.commit()
    if mode == "direct":
        runtime.record_receipt(claim, status="completed", evidence_digest=evidence, now=NOW)
        runtime.finish(work, state="completed", evidence_digest=evidence, now=NOW)
    elif mode == "lease-recovery":
        runtime.record_receipt(claim, status="completed", evidence_digest=evidence, now=NOW)
        sweep = runtime.recover_orphans(lambda _pid: "different-incarnation", now=NOW)
        assert sweep.finalized == 1
    elif mode == "direct-reconciled":
        runtime.record_receipt(claim, status="unknown", evidence_digest=evidence, now=NOW)
        runtime.finish(work, state="recovery-required", now=NOW)
        with sqlite3.connect(path) as db:
            case_id = str(
                db.execute(
                    "select id from local_recovery_case where effect_claim_id=?", (claim.id,)
                ).fetchone()[0]
            )
        runtime.resolve_recovery(case_id, outcome="completed", evidence_digest=evidence, now=NOW)
        runtime.reconcile_recovery(frozen.job_id, now=NOW)
    elif mode == "lease-reconciled":
        sweep = runtime.recover_orphans(lambda _pid: "different-incarnation", now=NOW)
        assert sweep.recovery_required == 1
        with sqlite3.connect(path) as db:
            case_id = str(
                db.execute(
                    "select id from local_recovery_case where effect_claim_id=?", (claim.id,)
                ).fetchone()[0]
            )
        runtime.resolve_recovery(case_id, outcome="completed", evidence_digest=evidence, now=NOW)
        runtime.reconcile_recovery(frozen.job_id, now=NOW)
    else:
        raise AssertionError(mode)
    with sqlite3.connect(path) as db:
        outboxes = db.execute(
            "select id,event_kind from local_outbox where job_id=? order by id",
            (frozen.job_id,),
        ).fetchall()
    expected_count = 4 if mode.endswith("reconciled") else 3
    assert len(outboxes) == expected_count
    for index, (outbox_id, kind) in enumerate(outboxes):
        claimed = runtime.claim_outbox(
            supported_kinds=(str(kind),),
            outbox_id=str(outbox_id),
            require_completed_job=True,
            owner_id="delivery",
            owner_pid=102,
            owner_token="delivery-incarnation",
            lease_seconds=30,
            now=NOW,
        )
        assert claimed is not None
        delivery_evidence = (
            frozen.delivery_evidence(_binding())
            if outbox_id == frozen.outbox_id
            else digest({"outbox_id": outbox_id, "delivered": True})
        )
        if resolved_unknown_delivery and index == 0:
            runtime.record_outbox_receipt(
                claimed, status="unknown", evidence_digest=delivery_evidence, now=NOW
            )
            with sqlite3.connect(path) as db:
                delivery_case = str(
                    db.execute(
                        "select id from local_recovery_case where outbox_id=?", (outbox_id,)
                    ).fetchone()[0]
                )
            runtime.resolve_recovery(
                delivery_case,
                outcome="delivered",
                evidence_digest=digest("delivery-resolution"),
                now=NOW,
            )
        else:
            runtime.record_outbox_receipt(
                claimed, status="delivered", evidence_digest=delivery_evidence, now=NOW
            )


def _resolved_hook_recovery(path: Path, frozen_revision: str) -> ExactResolvedRecovery:
    case_id = "018f0000-0000-7000-8000-000000000007"
    resolution_id = "018f0000-0000-7000-8000-000000000008"
    evidence = digest("hook-recovery-evidence")
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys=on")
        predecessor = db.execute(
            "select * from continuity_hook_attachment_revision where revision_digest=?",
            (frozen_revision,),
        ).fetchone()
        assert predecessor is not None
        case_body = {
            "attachment_id": ATTACHMENT_ID,
            "case_kind": "transaction-unknown",
            "created_at": NOW,
            "evidence_digest": evidence,
            "process_generation_digest": predecessor["process_generation_digest"],
            "recovery_case_id": case_id,
            "session_id": SESSION_ID,
        }
        db.execute(
            "insert into continuity_hook_recovery_case values(?,?,?,?,?,?,?,?)",
            (
                case_id,
                ATTACHMENT_ID,
                SESSION_ID,
                predecessor["process_generation_digest"],
                "transaction-unknown",
                evidence,
                canonical_json(case_body),
                NOW,
            ),
        )
        recovery_body = SQLiteDormantV4CloseWriter._revision_body(
            predecessor,
            revision_number=int(predecessor["revision_number"]) + 1,
            operation_key="recovery-required",
            state="recovery-required",
            created_at=NOW,
            checkpoint_digest=str(predecessor["checkpoint_digest"]),
            close_request_digest=str(predecessor["close_request_digest"]),
            pre_close_event_digest=str(predecessor["pre_close_event_digest"]),
        )
        recovery_body["hook_recovery_case_id"] = case_id
        recovery_revision = SQLiteDormantV4CloseWriter._insert_revision(db, recovery_body)
        resolution_body = {
            "created_at": NOW,
            "evidence_digest": evidence,
            "outcome": "restored",
            "recovery_case_id": case_id,
            "resolution_id": resolution_id,
        }
        db.execute(
            "insert into continuity_hook_recovery_resolution values(?,?,?,?,?,?)",
            (
                resolution_id,
                case_id,
                "restored",
                evidence,
                canonical_json(resolution_body),
                NOW,
            ),
        )
        db.commit()
    return ExactResolvedRecovery(recovery_revision, "hook", case_id, resolution_id, "restored", NOW)


def test_freeze_v1_is_atomic_and_exactly_replayable(tmp_path: Path) -> None:
    path = tmp_path / "operational-v4.db"
    seed = _seed(path)
    source = _Source()
    spool = _Spool(
        FrozenSpoolSnapshot(
            SESSION_ID,
            "external-session",
            "codex",
            (str(seed["spool_digest"]),),
        )
    )
    writer = SQLiteDormantV4CloseWriter(
        path, source=source, spool=spool, projections=_Projections()
    )

    frozen = writer.freeze_with_preclose(_request(seed))
    replay = writer.freeze_with_preclose(_request(seed))

    assert replay == frozen
    assert frozen.state == "pending"
    assert source.checks == 6
    assert source.resolves == 3
    assert spool.handle.checks == 3
    with sqlite3.connect(path) as db:
        assert db.execute("select status from session where id=?", (SESSION_ID,)).fetchone() == (
            "closing",
        )
        assert db.execute("select count(*) from session_event").fetchone() == (3,)
        assert db.execute("select count(*) from continuity_checkpoint").fetchone() == (1,)
        assert db.execute("select count(*) from continuity_close_request").fetchone() == (1,)
        assert db.execute("select count(*) from local_job").fetchone() == (1,)
        assert db.execute("select count(*) from local_outbox").fetchone() == (2,)


def test_freeze_explicit_empty_v2_preserves_six_projection_contract(tmp_path: Path) -> None:
    path = tmp_path / "operational-v4.db"
    seed = _seed(path)
    spool = _Spool(
        FrozenSpoolSnapshot(
            SESSION_ID,
            "external-session",
            "codex",
            (str(seed["spool_digest"]),),
        )
    )
    writer = SQLiteDormantV4CloseWriter(
        path, source=_Source(), spool=spool, projections=_Projections()
    )

    frozen = writer.freeze_with_preclose(_request(seed, candidates=CloseCandidateBundle()))

    assert frozen.input_body["schema"] == "zekam-local-close/v2"
    assert len(frozen.projections(_binding())) == 6


def test_source_recheck_failure_rolls_back_every_freeze_row(tmp_path: Path) -> None:
    path = tmp_path / "operational-v4.db"
    seed = _seed(path)
    source = _Source()
    source.fail = True
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=source,
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )

    with pytest.raises(PolicyViolation, match="source drift"):
        writer.freeze_with_preclose(_request(seed))

    with sqlite3.connect(path) as db:
        assert db.execute("select status from session where id=?", (SESSION_ID,)).fetchone() == (
            "open",
        )
        assert db.execute("select count(*) from continuity_close_request").fetchone() == (0,)
        assert db.execute("select count(*) from local_job").fetchone() == (0,)
        assert db.execute("select count(*) from session_event").fetchone() == (1,)


@pytest.mark.parametrize(
    "resolve_mode",
    ("exception", "os-error", "timeout", "wrong-type", "wrong-id", "wrong-bytes"),
)
def test_source_resolver_failure_or_drift_rolls_back_freeze(
    tmp_path: Path, resolve_mode: str
) -> None:
    path = tmp_path / f"resolver-{resolve_mode}.db"
    seed = _seed(path)
    source = _Source()
    source.resolve_mode = resolve_mode
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=source,
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )

    with pytest.raises(PolicyViolation):
        writer.freeze_with_preclose(_request(seed))

    with sqlite3.connect(path) as db:
        assert db.execute("select status from session").fetchone() == ("open",)
        assert db.execute("select count(*) from continuity_close_request").fetchone() == (0,)
        assert db.execute("select count(*) from local_job").fetchone() == (0,)


@pytest.mark.parametrize("snapshot_mode", ("wrong-type", "wrong-id", "wrong-revision"))
def test_source_snapshot_scope_drift_rejects_without_mutation(
    tmp_path: Path, snapshot_mode: str
) -> None:
    path = tmp_path / f"snapshot-{snapshot_mode}.db"
    seed = _seed(path)
    source = _Source()
    source.snapshot_mode = snapshot_mode
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=source,
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )

    with pytest.raises((PolicyViolation, ValidationFailed)):
        writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone() == (0,)


@pytest.mark.parametrize("drift", ("manifest-column", "newer-hydration"))
def test_manifest_or_latest_hydration_drift_rejects_without_close_rows(
    tmp_path: Path, drift: str
) -> None:
    path = tmp_path / f"manifest-{drift}.db"
    seed = _seed(path)
    with sqlite3.connect(path) as db:
        if drift == "manifest-column":
            trigger_sql = str(
                db.execute(
                    "select sql from sqlite_master where type='trigger' "
                    "and name='context_manifest_immutable_update'"
                ).fetchone()[0]
            )
            db.execute("drop trigger context_manifest_immutable_update")
            db.execute(
                "update context_manifest set token_count=token_count-1 where manifest_digest=?",
                (seed["manifest_digest"],),
            )
            db.execute(trigger_sql)
        else:
            second = digest(
                {
                    "session_id": SESSION_ID,
                    "manifest_digest": seed["manifest_digest"],
                    "idempotency_key": "hydrate-newer",
                    "grants_authority": False,
                }
            )
            db.execute(
                "insert into hydration_receipt values(?,?,?,?,?)",
                (
                    second,
                    SESSION_ID,
                    seed["manifest_digest"],
                    "hydrate-newer",
                    "2026-09-03T12:00:01+00:00",
                ),
            )
        db.commit()
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )

    with pytest.raises(PolicyViolation):
        writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone() == (0,)


def test_strict_manifest_fixed_vector_matches_v3_and_rejects_same_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest-v3-differential.db"
    seed = _seed(path)
    source_text = (Path("/Users/mkaracan/Projeler/akilli-kasa") / SOURCE_REF).read_text()
    monkeypatch.setattr(continuity_module, "SCHEMA_VERSION", 4)
    continuity = SQLiteContinuityStore(
        path, source_resolver=lambda _binding, _provenance: source_text
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert continuity._verified_manifest(db, _binding(), str(seed["manifest_digest"]))[
            "context"
        ]
    _unsafe_fixture_mutation(
        path,
        trigger_names=("context_manifest_immutable_update",),
        statement="update context_manifest set token_count=token_count-1 where manifest_digest=?",
        parameters=(seed["manifest_digest"],),
    )
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(PolicyViolation):
            continuity._verified_manifest(db, _binding(), str(seed["manifest_digest"]))
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    with pytest.raises(PolicyViolation):
        writer.freeze_with_preclose(_request(seed))


def test_capacity_and_unrelated_pending_work_fail_before_close_graph(tmp_path: Path) -> None:
    path = tmp_path / "capacity.db"
    seed = _seed(path, capacity=1)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    with pytest.raises(PolicyViolation, match="capacity"):
        writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone() == (0,)


def test_wrong_tail_and_spool_delta_reject_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "tail.db"
    seed = _seed(path)
    wrong_spool = _Spool(
        FrozenSpoolSnapshot(
            SESSION_ID,
            "external-session",
            "codex",
            (str(seed["spool_digest"]), digest("unpersisted")),
        )
    )
    writer = SQLiteDormantV4CloseWriter(
        path, source=_Source(), spool=wrong_spool, projections=_Projections()
    )
    with pytest.raises(PolicyViolation, match="unpersisted spool"):
        writer.freeze_with_preclose(_request(seed))
    request = _request(seed)
    wrong_tail = FrozenCloseWriteRequest(
        request.binding,
        request.expected_attachment_revision_digest,
        request.expected_process_generation_digest,
        ContinuityTail(1, digest("fork")),
        request.active_manifest_digest,
        request.checkpoint_idempotency_key,
        request.operation_key,
        request.summary,
        request.candidates,
        request.observed_at,
    )
    good_writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    with pytest.raises(ConcurrencyConflict, match="tail drift"):
        good_writer.freeze_with_preclose(wrong_tail)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone() == (0,)


def test_two_concurrent_identical_freezers_converge_on_one_graph(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    seed = _seed(path)
    spool = _Spool(
        FrozenSpoolSnapshot(
            SESSION_ID,
            "external-session",
            "codex",
            (str(seed["spool_digest"]),),
        )
    )

    def run() -> FrozenClose:
        return SQLiteDormantV4CloseWriter(
            path, source=_Source(), spool=spool, projections=_Projections()
        ).freeze_with_preclose(_request(seed))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: run(), range(2)))

    assert results[0] == results[1]
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone() == (1,)
        assert db.execute("select count(*) from local_job").fetchone() == (1,)


@pytest.mark.parametrize("target", ("producer-receipt", "checkpoint"))
def test_freeze_replay_rejects_low_level_immutable_graph_tamper(
    tmp_path: Path, target: str
) -> None:
    path = tmp_path / f"tamper-{target}.db"
    seed = _seed(path)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    writer.freeze_with_preclose(_request(seed))
    if target == "producer-receipt":
        _unsafe_fixture_mutation(
            path,
            trigger_names=("continuity_internal_event_no_update",),
            statement="update continuity_internal_event_receipt set body_json='{}' "
            "where event_kind='PRE_CLOSE'",
        )
    else:
        _unsafe_fixture_mutation(
            path,
            trigger_names=("continuity_checkpoint_immutable_update",),
            statement="update continuity_checkpoint set spool_digest=?",
            parameters=(digest("corrupt-spool"),),
        )

    with pytest.raises(PolicyViolation):
        writer.freeze_with_preclose(_request(seed))


def test_finalize_rejects_missing_exact_compile_delivery_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "missing-delivery-receipt.db"
    seed = _seed(path)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    frozen = writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        frozen_revision = str(
            db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "where state='frozen'"
            ).fetchone()[0]
        )
    _materialize_and_complete(path, frozen, monkeypatch)
    _unsafe_fixture_mutation(
        path,
        trigger_names=("local_outbox_receipt_no_delete",),
        statement="delete from local_outbox_receipt where outbox_id=?",
        parameters=(frozen.outbox_id,),
    )

    with pytest.raises(PolicyViolation):
        writer.finalize_with_session_closed(
            FinalizeClosedWriteRequest(
                _binding(), frozen.request_digest, frozen_revision, "finalize", NOW
            )
        )
    with sqlite3.connect(path) as db:
        assert db.execute("select status from session").fetchone() == ("closing",)
        assert db.execute("select count(*) from close_receipt").fetchone() == (0,)


def test_progressed_freeze_replay_rejects_terminal_outbox_payload_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "terminal-outbox-drift.db"
    seed = _seed(path)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    frozen = writer.freeze_with_preclose(_request(seed))
    _materialize_and_complete(path, frozen, monkeypatch)
    corrupt = {"job_id": frozen.job_id, "state": "failed"}
    _unsafe_fixture_mutation(
        path,
        trigger_names=("local_outbox_no_update",),
        statement="update local_outbox set payload_json=?,payload_digest=? where idempotency_key=?",
        parameters=(
            canonical_json(corrupt),
            digest(corrupt),
            f"job:{frozen.job_id}:terminal",
        ),
    )

    with pytest.raises(PolicyViolation):
        writer.freeze_with_preclose(_request(seed))


def test_v4_ddl_rejects_unrelated_pending_session_job_after_freeze(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.db"
    seed = _seed(path)
    spool = _Spool(
        FrozenSpoolSnapshot(
            SESSION_ID,
            "external-session",
            "codex",
            (str(seed["spool_digest"]),),
        )
    )
    writer = SQLiteDormantV4CloseWriter(
        path, source=_Source(), spool=spool, projections=_Projections()
    )
    writer.freeze_with_preclose(_request(seed))
    payload = {
        "operation": "unrelated",
        "session_id": SESSION_ID,
    }
    with (
        sqlite3.connect(path) as db,
        pytest.raises(sqlite3.IntegrityError, match="admission frozen"),
    ):
        db.execute(
            "insert into local_job(id,idempotency_key,payload_json,state,attempt_count,"
            "max_attempts,available_at,timeout_at,fencing_counter,terminal_evidence_digest,"
            "created_at,updated_at) values(?,?,?,'ready',0,1,?,null,0,null,?,?)",
            ("unrelated-job", "unrelated-key", canonical_json(payload), NOW, NOW, NOW),
        )
    assert writer.freeze_with_preclose(_request(seed)).request_digest


@pytest.mark.parametrize(
    ("candidates", "projection_count"),
    [(None, 2), (CloseCandidateBundle(), 6)],
)
def test_direct_compile_delivery_and_finalize_are_atomic_and_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidates: CloseCandidateBundle | None,
    projection_count: int,
) -> None:
    path = tmp_path / "operational-v4.db"
    seed = _seed(path)
    source = _Source()
    spool = _Spool(
        FrozenSpoolSnapshot(
            SESSION_ID,
            "external-session",
            "codex",
            (str(seed["spool_digest"]),),
        )
    )
    writer = SQLiteDormantV4CloseWriter(
        path, source=source, spool=spool, projections=_Projections()
    )
    frozen = writer.freeze_with_preclose(_request(seed, candidates=candidates))
    assert len(frozen.projections(_binding())) == projection_count
    with sqlite3.connect(path) as db:
        frozen_revision = str(
            db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "where state='frozen'"
            ).fetchone()[0]
        )
    _materialize_and_complete(path, frozen, monkeypatch)
    final = FinalizeClosedWriteRequest(
        _binding(), frozen.request_digest, frozen_revision, "finalize", NOW
    )

    receipt = writer.finalize_with_session_closed(final)
    replay = SQLiteDormantV4CloseWriter(
        path, source=source, spool=spool, projections=_Projections()
    ).finalize_with_session_closed(final)

    assert replay == receipt
    with sqlite3.connect(path) as db:
        assert db.execute(
            "select status,closed_at,close_receipt_digest from session where id=?",
            (SESSION_ID,),
        ).fetchone() == ("closed", NOW, receipt)
        assert db.execute(
            "select count(*) from session_event where event_kind='SESSION_CLOSED'"
        ).fetchone() == (1,)
        assert db.execute(
            "select count(*) from continuity_hook_attachment_revision where state='closed'"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    "mode",
    ("direct", "lease-recovery", "direct-reconciled", "lease-reconciled"),
)
@pytest.mark.parametrize("resolved_unknown_delivery", (False, True))
def test_runtime_monotone_terminal_variants_finalize_and_closed_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    resolved_unknown_delivery: bool,
) -> None:
    path = tmp_path / f"{mode}-{resolved_unknown_delivery}.db"
    seed = _seed(path)
    source = _Source()
    spool = _Spool(
        FrozenSpoolSnapshot(
            SESSION_ID,
            "external-session",
            "codex",
            (str(seed["spool_digest"]),),
        )
    )
    writer = SQLiteDormantV4CloseWriter(
        path, source=source, spool=spool, projections=_Projections()
    )
    frozen = writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        frozen_revision = str(
            db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "where state='frozen'"
            ).fetchone()[0]
        )
    _materialize_runtime_variant(
        path,
        frozen,
        monkeypatch,
        mode=mode,
        resolved_unknown_delivery=resolved_unknown_delivery,
    )
    progressed = writer.freeze_with_preclose(_request(seed))
    assert progressed.state == "pending"
    final = FinalizeClosedWriteRequest(
        _binding(), frozen.request_digest, frozen_revision, "finalize", NOW
    )

    receipt = writer.finalize_with_session_closed(final)
    assert writer.finalize_with_session_closed(final) == receipt

    with sqlite3.connect(path) as db:
        assert db.execute(
            "select status,close_receipt_digest from session where id=?", (SESSION_ID,)
        ).fetchone() == ("closed", receipt)


@pytest.mark.parametrize(
    ("runtime_state", "expected_state"),
    (
        ("running", "pending"),
        ("failed", "recovery-required"),
        ("recovery-required", "recovery-required"),
        ("quarantined", "recovery-required"),
    ),
)
def test_progressed_non_success_runtime_states_replay_without_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_state: str,
    expected_state: str,
) -> None:
    path = tmp_path / f"progress-{runtime_state}.db"
    seed = _seed(path)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    frozen = writer.freeze_with_preclose(_request(seed))
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=30,
        now=NOW,
    )
    assert work is not None
    if runtime_state == "quarantined":
        runtime.quarantine(work, evidence_digest=digest("quarantine"), now=NOW)
    elif runtime_state != "running":
        evidence = frozen.compile_evidence(_binding())
        claim, _ = runtime.claim_effect(
            work,
            operation="continuity.compile",
            effect_digest=evidence,
            idempotency_key=frozen.effect_key,
            now=NOW,
        )
        with sqlite3.connect(path) as db:
            db.execute("pragma foreign_keys=on")
            db.execute(
                "insert into continuity_effect_binding values(?,?,?,?)",
                (claim.id, SESSION_ID, frozen.job_id, _binding().binding_digest),
            )
            db.commit()
        if runtime_state == "failed":
            runtime.record_receipt(claim, status="failed", evidence_digest=evidence, now=NOW)
            runtime.finish(work, state="failed", evidence_digest=evidence, now=NOW)
        else:
            runtime.finish(work, state="recovery-required", now=NOW)

    monkeypatch.setattr(
        SQLiteDormantV4CloseWriter,
        "_trusted_now",
        staticmethod(lambda: dt.datetime(2026, 9, 3, 12, 0, 1, tzinfo=dt.UTC)),
    )
    replay = writer.freeze_with_preclose(_request(seed))
    assert replay.state == expected_state


def test_closed_replay_rejects_low_level_terminal_receipt_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "closed-receipt-drift.db"
    seed = _seed(path)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    frozen = writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        frozen_revision = str(
            db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "where state='frozen'"
            ).fetchone()[0]
        )
    _materialize_and_complete(path, frozen, monkeypatch)
    final = FinalizeClosedWriteRequest(
        _binding(), frozen.request_digest, frozen_revision, "finalize", NOW
    )
    writer.finalize_with_session_closed(final)
    _unsafe_fixture_mutation(
        path,
        trigger_names=("close_receipt_immutable_update",),
        statement="update close_receipt set projections_json='[]'",
    )

    with pytest.raises(PolicyViolation):
        writer.finalize_with_session_closed(final)
    with pytest.raises(PolicyViolation):
        writer.freeze_with_preclose(_request(seed))


def test_finalize_without_terminal_compile_evidence_leaves_session_closing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "terminal-missing.db"
    seed = _seed(path)
    spool = _Spool(
        FrozenSpoolSnapshot(
            SESSION_ID,
            "external-session",
            "codex",
            (str(seed["spool_digest"]),),
        )
    )
    writer = SQLiteDormantV4CloseWriter(
        path, source=_Source(), spool=spool, projections=_Projections()
    )
    frozen = writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        frozen_revision = str(
            db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "where state='frozen'"
            ).fetchone()[0]
        )
    request = FinalizeClosedWriteRequest(
        _binding(), frozen.request_digest, frozen_revision, "finalize", NOW
    )

    with pytest.raises(PolicyViolation, match="completed compile evidence"):
        writer.finalize_with_session_closed(request)

    with sqlite3.connect(path) as db:
        assert db.execute(
            "select status,close_receipt_digest from session where id=?", (SESSION_ID,)
        ).fetchone() == ("closing", None)
        assert db.execute("select count(*) from close_receipt").fetchone() == (0,)
        assert db.execute(
            "select count(*) from continuity_hook_attachment_revision where state='closed'"
        ).fetchone() == (0,)


def test_projection_drift_after_terminal_inserts_rolls_back_entire_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "projection-drift.db"
    seed = _seed(path)
    spool = _Spool(
        FrozenSpoolSnapshot(
            SESSION_ID,
            "external-session",
            "codex",
            (str(seed["spool_digest"]),),
        )
    )
    setup_writer = SQLiteDormantV4CloseWriter(
        path, source=_Source(), spool=spool, projections=_Projections()
    )
    frozen = setup_writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        frozen_revision = str(
            db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "where state='frozen'"
            ).fetchone()[0]
        )
    _materialize_and_complete(path, frozen, monkeypatch)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=spool,
        projections=_Projections(fail_after=2),
    )

    with pytest.raises(PolicyViolation, match="projection drift"):
        writer.finalize_with_session_closed(
            FinalizeClosedWriteRequest(
                _binding(), frozen.request_digest, frozen_revision, "finalize", NOW
            )
        )

    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from close_receipt").fetchone() == (0,)
        assert db.execute(
            "select status,close_receipt_digest from session where id=?", (SESSION_ID,)
        ).fetchone() == ("closing", None)
        assert db.execute(
            "select count(*) from session_event where event_kind='SESSION_CLOSED'"
        ).fetchone() == (0,)


def test_finalize_consumes_exact_persisted_hook_recovery_without_manufacturing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "operational-v4.db"
    seed = _seed(path)
    spool = _Spool(
        FrozenSpoolSnapshot(
            SESSION_ID,
            "external-session",
            "codex",
            (str(seed["spool_digest"]),),
        )
    )
    writer = SQLiteDormantV4CloseWriter(
        path, source=_Source(), spool=spool, projections=_Projections()
    )
    frozen = writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        frozen_revision = str(
            db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "where state='frozen'"
            ).fetchone()[0]
        )
    _materialize_and_complete(path, frozen, monkeypatch)
    recovery = _resolved_hook_recovery(path, frozen_revision)

    receipt = writer.finalize_with_session_closed(
        FinalizeClosedWriteRequest(
            _binding(),
            frozen.request_digest,
            frozen_revision,
            "recover-finalize",
            NOW,
            recovery,
        )
    )
    replay_request = FinalizeClosedWriteRequest(
        _binding(),
        frozen.request_digest,
        frozen_revision,
        "recover-finalize",
        NOW,
        recovery,
    )
    assert writer.finalize_with_session_closed(replay_request) == receipt

    with sqlite3.connect(path) as db:
        assert db.execute(
            "select count(*) from session_event where event_kind='CRASH_RECOVERED'"
        ).fetchone() == (1,)
        assert db.execute(
            "select count(*) from continuity_hook_recovery_resolution"
        ).fetchone() == (1,)
        assert db.execute(
            "select status,close_receipt_digest from session where id=?", (SESSION_ID,)
        ).fetchone() == ("closed", receipt)


def test_fresh_freeze_rejects_open_hook_recovery_for_current_generation(tmp_path: Path) -> None:
    path = tmp_path / "open-hook-recovery.db"
    seed = _seed(path)
    recovery_id = "018f0000-0000-7000-8000-000000000099"
    body = {
        "attachment_id": ATTACHMENT_ID,
        "case_kind": "transaction-unknown",
        "created_at": NOW,
        "evidence_digest": digest("open-hook-case"),
        "process_generation_digest": seed["generation_digest"],
        "recovery_case_id": recovery_id,
        "session_id": SESSION_ID,
    }
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into continuity_hook_recovery_case values(?,?,?,?,?,?,?,?)",
            (
                recovery_id,
                ATTACHMENT_ID,
                SESSION_ID,
                seed["generation_digest"],
                "transaction-unknown",
                body["evidence_digest"],
                canonical_json(body),
                NOW,
            ),
        )
        db.commit()

    with pytest.raises(PolicyViolation, match="pending work"):
        SQLiteDormantV4CloseWriter(
            path,
            source=_Source(),
            spool=_Spool(
                FrozenSpoolSnapshot(
                    SESSION_ID,
                    "external-session",
                    "codex",
                    (str(seed["spool_digest"]),),
                )
            ),
            projections=_Projections(),
        ).freeze_with_preclose(_request(seed))


def test_replay_rebuilds_current_revision_body_and_digest(tmp_path: Path) -> None:
    path = tmp_path / "revision-digest-drift.db"
    seed = _seed(path)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    writer.freeze_with_preclose(_request(seed))
    _unsafe_fixture_mutation(
        path,
        trigger_names=("continuity_hook_revision_no_update",),
        statement=(
            "update continuity_hook_attachment_revision set revision_digest=? where state='frozen'"
        ),
        parameters=(digest("forged-frozen-revision"),),
    )

    with pytest.raises(PolicyViolation, match="revision body/column/digest drift"):
        writer.freeze_with_preclose(_request(seed))


def test_no_effect_lease_recovery_is_exact_attention_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "no-effect-recovery.db"
    seed = _seed(path)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    frozen = writer.freeze_with_preclose(_request(seed))
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    assert (
        runtime.claim_next(
            supported_operations=("continuity.compile",),
            job_id=frozen.job_id,
            owner_id="worker",
            owner_pid=101,
            owner_token="incarnation",
            lease_seconds=30,
            now=NOW,
        )
        is not None
    )
    assert runtime.recover_orphans(lambda _pid: "different", now=NOW).requeued == 1

    replay = writer.freeze_with_preclose(_request(seed))
    assert replay.state == "recovery-required"


def test_replay_rejects_running_job_and_claimed_delivery_expired_by_system_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "expired-runtime-authority.db"
    seed = _seed(path)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    frozen = writer.freeze_with_preclose(_request(seed))
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=1,
        now=NOW,
    )
    assert work is not None
    with sqlite3.connect(path) as db:
        db.execute(
            "update local_lease set expires_at='2020-01-01T00:00:01+00:00' where job_id=?",
            (frozen.job_id,),
        )
        db.commit()
    with pytest.raises(PolicyViolation, match="running job owner/fence drift"):
        writer.freeze_with_preclose(_request(seed))

    path = tmp_path / "expired-delivery-authority.db"
    seed = _seed(path)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    frozen = writer.freeze_with_preclose(_request(seed))
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    claimed = runtime.claim_outbox(
        supported_kinds=("job.enqueued",),
        outbox_id=None,
        require_completed_job=False,
        owner_id="delivery",
        owner_pid=102,
        owner_token="incarnation",
        lease_seconds=1,
        now=NOW,
    )
    assert claimed is not None
    with sqlite3.connect(path) as db:
        db.execute(
            "update local_outbox_delivery set expires_at='2020-01-01T00:00:01+00:00' "
            "where outbox_id=?",
            (claimed.event.id,),
        )
        db.commit()
    with pytest.raises(PolicyViolation, match="delivery timestamp order drift"):
        writer.freeze_with_preclose(_request(seed))


def test_completed_job_with_pending_deliveries_remains_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "completed-pending-delivery.db"
    seed = _seed(path)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_Source(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )
    frozen = writer.freeze_with_preclose(_request(seed))
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=30,
        now=NOW,
    )
    assert work is not None
    evidence = frozen.compile_evidence(_binding())
    claim, created = runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=evidence,
        idempotency_key=frozen.effect_key,
        now=NOW,
    )
    assert created
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into continuity_effect_binding values(?,?,?,?)",
            (claim.id, SESSION_ID, frozen.job_id, _binding().binding_digest),
        )
        db.commit()
    runtime.record_receipt(claim, status="completed", evidence_digest=evidence, now=NOW)
    runtime.finish(work, state="completed", evidence_digest=evidence, now=NOW)

    assert writer.freeze_with_preclose(_request(seed)).state == "pending"


class _UnexpectedResolver(_Source):
    def resolve_fragment(
        self,
        binding: ContinuityBinding,
        snapshot: CurrentSourceSnapshot,
        provenance: CanonicalManifestProvenance,
    ) -> ResolvedManifestFragment:
        raise RuntimeError("unexpected local resolver bug")


def test_unexpected_resolver_exception_is_typed_and_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "unexpected-resolver.db"
    seed = _seed(path)
    writer = SQLiteDormantV4CloseWriter(
        path,
        source=_UnexpectedResolver(),
        spool=_Spool(
            FrozenSpoolSnapshot(
                SESSION_ID,
                "external-session",
                "codex",
                (str(seed["spool_digest"]),),
            )
        ),
        projections=_Projections(),
    )

    with pytest.raises(PolicyViolation, match="source fragment unavailable"):
        writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        assert db.execute("select status from session").fetchone() == ("open",)
        assert db.execute("select count(*) from continuity_close_request").fetchone() == (0,)


def test_finalize_acquires_projection_lock_before_authoritative_source_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "finalize-lock-order.db"
    seed = _seed(path)
    spool = _Spool(
        FrozenSpoolSnapshot(
            SESSION_ID,
            "external-session",
            "codex",
            (str(seed["spool_digest"]),),
        )
    )
    setup = SQLiteDormantV4CloseWriter(
        path, source=_Source(), spool=spool, projections=_Projections()
    )
    frozen = setup.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        frozen_revision = str(
            db.execute(
                "select revision_digest from continuity_hook_attachment_revision "
                "where state='frozen'"
            ).fetchone()[0]
        )
    _materialize_and_complete(path, frozen, monkeypatch)
    order: list[str] = []

    class OrderedSource(_Source):
        def snapshot(self, binding: ContinuityBinding) -> CurrentSourceSnapshot:
            order.append("source")
            return super().snapshot(binding)

    class OrderedProjections(_Projections):
        @contextlib.contextmanager
        def frozen(self, request: FrozenClose) -> Iterator[_ProjectionHandle]:
            order.append("projection")
            with super().frozen(request) as handle:
                yield handle

    writer = SQLiteDormantV4CloseWriter(
        path, source=OrderedSource(), spool=spool, projections=OrderedProjections()
    )
    writer.finalize_with_session_closed(
        FinalizeClosedWriteRequest(
            _binding(), frozen.request_digest, frozen_revision, "finalize", NOW
        )
    )

    assert order[0:2] == ["projection", "source"]


def test_replay_pending_scope_uses_effect_binding_when_job_payload_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bound-effect-payload-drift.db"
    seed = _seed(path)
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    payload = {
        "operation": "continuity.compile",
        "session_id": SESSION_ID,
        "run_id": None,
        "binding_digest": _binding().binding_digest,
    }
    job, created = runtime.enqueue(
        idempotency_key="preexisting-session-compile",
        payload=payload,
        available_at=NOW,
    )
    assert created
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=job.id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=30,
        now=NOW,
    )
    assert work is not None
    effect_evidence = digest("preexisting-effect")
    claim, created = runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=effect_evidence,
        idempotency_key="preexisting-effect",
        now=NOW,
    )
    assert created
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into continuity_effect_binding values(?,?,?,?)",
            (claim.id, SESSION_ID, job.id, _binding().binding_digest),
        )
        db.commit()
    receipt = runtime.record_receipt(
        claim, status="completed", evidence_digest=effect_evidence, now=NOW
    )
    runtime.finish(work, state="completed", evidence_digest=effect_evidence, now=NOW)
    with sqlite3.connect(path) as db:
        outboxes = db.execute(
            "select id,event_kind from local_outbox where job_id=? order by id", (job.id,)
        ).fetchall()
    for outbox_id, kind in outboxes:
        delivery = runtime.claim_outbox(
            supported_kinds=(str(kind),),
            outbox_id=str(outbox_id),
            require_completed_job=True,
            owner_id="delivery",
            owner_pid=102,
            owner_token="delivery-incarnation",
            lease_seconds=30,
            now=NOW,
        )
        assert delivery is not None
        runtime.record_outbox_receipt(
            delivery,
            status="delivered",
            evidence_digest=digest({"outbox_id": outbox_id, "delivered": True}),
            now=NOW,
        )
    writer = _v4_writer(path, seed)
    writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        trigger = str(
            db.execute(
                "select sql from sqlite_master where type='trigger' "
                "and name='local_effect_receipt_no_delete'"
            ).fetchone()[0]
        )
        db.execute("drop trigger local_effect_receipt_no_delete")
        db.execute("delete from local_effect_receipt where id=?", (receipt.id,))
        db.execute(
            "update local_job set payload_json=? where id=?",
            (canonical_json({**payload, "session_id": "foreign-session"}), job.id),
        )
        db.execute(trigger)
        db.commit()

    with pytest.raises(PolicyViolation, match="unrelated pending work"):
        writer.freeze_with_preclose(_request(seed))


def test_replay_pending_scope_uses_outbox_binding_when_job_payload_is_foreign(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bound-outbox-payload-drift.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    job_id = "bound-foreign-job"
    outbox_id = "bound-foreign-outbox"
    payload = {
        "operation": "other",
        "session_id": SESSION_ID,
        "binding_digest": _binding().binding_digest,
    }
    outbox_payload = {"job_id": job_id, "state": "completed"}
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into local_job values(?,?,?,'completed',1,1,?,null,1,?,?,?)",
            (
                job_id,
                "bound-foreign-job-key",
                canonical_json(payload),
                NOW,
                digest("bound-foreign-terminal"),
                NOW,
                NOW,
            ),
        )
        db.execute(
            "insert into local_outbox values(?,?,?,?,?,?,?)",
            (
                outbox_id,
                job_id,
                "bound-foreign-outbox-key",
                "job.completed",
                canonical_json(outbox_payload),
                digest(outbox_payload),
                NOW,
            ),
        )
        db.execute(
            "insert into local_outbox_delivery values(?,'delivered',1,?,?,?,?,?,?)",
            (
                outbox_id,
                "bound-foreign-claim",
                "delivery",
                102,
                "delivery-incarnation",
                NOW,
                NOW,
            ),
        )
        db.execute(
            "insert into continuity_outbox_binding values(?,?,?,'checkpoint',?,null)",
            (outbox_id, SESSION_ID, job_id, digest("bound-foreign-input")),
        )
        db.execute(
            "insert into local_outbox_receipt values(?,?,?,?,?,?,?)",
            (
                "bound-foreign-receipt",
                outbox_id,
                "bound-foreign-claim",
                1,
                "delivered",
                digest("bound-foreign-delivery"),
                NOW,
            ),
        )
        db.commit()
    writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        trigger = str(
            db.execute(
                "select sql from sqlite_master where type='trigger' "
                "and name='local_outbox_receipt_no_delete'"
            ).fetchone()[0]
        )
        db.execute("drop trigger local_outbox_receipt_no_delete")
        db.execute("delete from local_outbox_receipt where outbox_id=?", (outbox_id,))
        db.execute(
            "update local_outbox_delivery set state='pending',fencing_counter=0,"
            "claim_id=null,owner_id=null,owner_pid=null,owner_token=null,expires_at=null "
            "where outbox_id=?",
            (outbox_id,),
        )
        db.execute(
            "update local_job set payload_json=? where id=?",
            (canonical_json({**payload, "session_id": "foreign-session"}), job_id),
        )
        db.execute(trigger)
        db.commit()

    with pytest.raises(PolicyViolation, match="unrelated pending work"):
        writer.freeze_with_preclose(_request(seed))


def test_receiptless_compile_claim_cannot_form_failed_attention_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receiptless-failed-claim.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=30,
        now=NOW,
    )
    assert work is not None
    evidence = frozen.compile_evidence(_binding())
    claim, created = runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=evidence,
        idempotency_key=frozen.effect_key,
        now=NOW,
    )
    assert created
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into continuity_effect_binding values(?,?,?,?)",
            (claim.id, SESSION_ID, frozen.job_id, _binding().binding_digest),
        )
        db.execute("delete from local_lease where job_id=?", (frozen.job_id,))
        terminal = digest([(None, None, None, None)])
        db.execute(
            "update local_job set state='failed',terminal_evidence_digest=? where id=?",
            (terminal, frozen.job_id),
        )
        body = {"job_id": frozen.job_id, "state": "failed"}
        db.execute(
            "insert into local_outbox values(?,?,?,?,?,?,?)",
            (
                "receiptless-failed-outbox",
                frozen.job_id,
                f"job:{frozen.job_id}:terminal",
                "job.failed",
                canonical_json(body),
                digest(body),
                NOW,
            ),
        )
        db.execute(
            "insert into local_outbox_delivery(outbox_id,state,updated_at) values(?,'pending',?)",
            ("receiptless-failed-outbox", NOW),
        )
        db.commit()

    with pytest.raises(PolicyViolation, match="failed job effect evidence drift"):
        writer.freeze_with_preclose(_request(seed))


def test_ready_job_requires_pristine_attempt_and_fence(tmp_path: Path) -> None:
    path = tmp_path / "ready-progressed-counters.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    with sqlite3.connect(path) as db:
        db.execute(
            "update local_job set attempt_count=1,fencing_counter=1 where id=?",
            (frozen.job_id,),
        )
        db.commit()

    with pytest.raises(PolicyViolation, match="ready job graph drift"):
        writer.freeze_with_preclose(_request(seed))


def test_terminal_compile_claim_retains_exact_job_fencing_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "terminal-claim-fence-drift.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    _materialize_and_complete(path, frozen, monkeypatch)
    _unsafe_fixture_mutation(
        path,
        trigger_names=("local_effect_claim_no_update",),
        statement="update local_effect_claim set fencing_token=2 where job_id=?",
        parameters=(frozen.job_id,),
    )

    with pytest.raises(PolicyViolation, match="fencing generation drift"):
        writer.freeze_with_preclose(_request(seed))


def test_terminal_compile_claim_retains_canonical_removed_lease_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "terminal-claim-lease-identity-drift.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    _materialize_and_complete(path, frozen, monkeypatch)
    _unsafe_fixture_mutation(
        path,
        trigger_names=("local_effect_claim_no_update",),
        statement="update local_effect_claim set lease_id='' where job_id=?",
        parameters=(frozen.job_id,),
    )

    with pytest.raises(PolicyViolation, match="compile claim identity drift"):
        writer.freeze_with_preclose(_request(seed))


def test_failed_required_delivery_is_attention_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed-required-delivery.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=30,
        now=NOW,
    )
    assert work is not None
    evidence = frozen.compile_evidence(_binding())
    claim, created = runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=evidence,
        idempotency_key=frozen.effect_key,
        now=NOW,
    )
    assert created
    with sqlite3.connect(path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into continuity_effect_binding values(?,?,?,?)",
            (claim.id, SESSION_ID, frozen.job_id, _binding().binding_digest),
        )
        db.commit()
    runtime.record_receipt(claim, status="completed", evidence_digest=evidence, now=NOW)
    runtime.finish(work, state="completed", evidence_digest=evidence, now=NOW)
    with sqlite3.connect(path) as db:
        outbox_id = str(
            db.execute(
                "select id from local_outbox where job_id=? and event_kind='job.enqueued'",
                (frozen.job_id,),
            ).fetchone()[0]
        )
    delivery = runtime.claim_outbox(
        supported_kinds=("job.enqueued",),
        outbox_id=outbox_id,
        require_completed_job=True,
        owner_id="delivery",
        owner_pid=102,
        owner_token="delivery-incarnation",
        lease_seconds=30,
        now=NOW,
    )
    assert delivery is not None
    runtime.record_outbox_receipt(
        delivery,
        status="failed",
        evidence_digest=digest("known-delivery-failure"),
        now=NOW,
    )

    assert writer.freeze_with_preclose(_request(seed)).state == "recovery-required"


def test_replay_rebuilds_event_and_producer_timestamp_parity(tmp_path: Path) -> None:
    path = tmp_path / "event-created-at-drift.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    writer.freeze_with_preclose(_request(seed))
    _unsafe_fixture_mutation(
        path,
        trigger_names=("session_event_no_update",),
        statement=(
            "update session_event set created_at='2026-09-03T12:00:01+00:00' "
            "where event_kind='PRE_CLOSE'"
        ),
    )

    with pytest.raises(PolicyViolation, match="event chain integrity drift"):
        writer.freeze_with_preclose(_request(seed))


def test_native_ancestry_rebuilds_reviewed_command_artifact_parity(tmp_path: Path) -> None:
    path = tmp_path / "native-command-artifact-drift.db"
    seed = _seed(path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        names = (
            "continuity_hook_ancestry_no_update",
            "continuity_native_event_no_update",
        )
        sql = [
            str(
                db.execute(
                    "select sql from sqlite_master where type='trigger' and name=?", (name,)
                ).fetchone()[0]
            )
            for name in names
        ]
        for name in names:
            db.execute(f'drop trigger "{name}"')
        native = db.execute(
            "select * from continuity_native_event_receipt "
            "where internal_event_type='SESSION_START'"
        ).fetchone()
        assert native is not None
        ancestry = db.execute(
            "select * from continuity_hook_invocation_ancestry_receipt where receipt_digest=?",
            (native["ancestry_receipt_digest"],),
        ).fetchone()
        assert ancestry is not None
        replacement = digest("unreviewed-shell-artifact")
        ancestry_body = __import__("json").loads(ancestry["body_json"])
        ancestry_body["shell_artifact_digest"] = replacement
        ancestry_digest = digest(ancestry_body)
        native_body = __import__("json").loads(native["body_json"])
        native_body["shell_artifact_digest"] = replacement
        native_body["ancestry_receipt_digest"] = ancestry_digest
        native_digest = digest(native_body)
        db.execute(
            "update continuity_hook_invocation_ancestry_receipt set "
            "receipt_digest=?,shell_artifact_digest=?,body_json=? where receipt_digest=?",
            (
                ancestry_digest,
                replacement,
                canonical_json(ancestry_body),
                ancestry["receipt_digest"],
            ),
        )
        db.execute(
            "update continuity_native_event_receipt set receipt_digest=?,"
            "ancestry_receipt_digest=?,shell_artifact_digest=?,body_json=? where event_digest=?",
            (
                native_digest,
                ancestry_digest,
                replacement,
                canonical_json(native_body),
                native["event_digest"],
            ),
        )
        for statement in sql:
            db.execute(statement)
        db.commit()

    with pytest.raises(PolicyViolation, match="native producer integrity drift"):
        _v4_writer(path, seed).freeze_with_preclose(_request(seed))


@pytest.mark.parametrize("target", ("delivery", "outbox"))
def test_pending_initial_delivery_timestamp_is_frozen_input_timestamp(
    tmp_path: Path, target: str
) -> None:
    path = tmp_path / "pending-delivery-time-drift.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    if target == "delivery":
        with sqlite3.connect(path) as db:
            db.execute(
                "update local_outbox_delivery set updated_at='2026-09-03T12:00:01+00:00' "
                "where outbox_id=? and state='pending'",
                (frozen.outbox_id,),
            )
            db.commit()
        message = "pending delivery drift"
    else:
        _unsafe_fixture_mutation(
            path,
            trigger_names=("local_outbox_no_update",),
            statement=("update local_outbox set created_at='2026-09-03T12:00:01+00:00' where id=?"),
            parameters=(frozen.outbox_id,),
        )
        message = "immutable initial outbox drift"

    with pytest.raises(PolicyViolation, match=message):
        writer.freeze_with_preclose(_request(seed))


def test_running_compile_claim_cannot_postdate_lease_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "running-claim-after-lease-expiry.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=3600,
        now=NOW,
    )
    assert work is not None
    evidence = frozen.compile_evidence(_binding())
    claim, created = runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=evidence,
        idempotency_key=frozen.effect_key,
        now=NOW,
    )
    assert created
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into continuity_effect_binding values(?,?,?,?)",
            (claim.id, SESSION_ID, frozen.job_id, _binding().binding_digest),
        )
        db.commit()
    _unsafe_fixture_mutation(
        path,
        trigger_names=("local_effect_claim_no_update",),
        statement=(
            "update local_effect_claim set claimed_at='2026-09-03T14:00:00+00:00' where job_id=?"
        ),
        parameters=(frozen.job_id,),
    )
    monkeypatch.setattr(
        SQLiteDormantV4CloseWriter,
        "_trusted_now",
        staticmethod(lambda: dt.datetime(2026, 9, 3, 12, 0, 1, tzinfo=dt.UTC)),
    )

    with pytest.raises(PolicyViolation, match=r"claim.*lease"):
        writer.freeze_with_preclose(_request(seed))


def test_effect_recovery_case_requires_canonical_creation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "effect-recovery-case-time.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=3600,
        now=NOW,
    )
    assert work is not None
    evidence = frozen.compile_evidence(_binding())
    claim, created = runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=evidence,
        idempotency_key=frozen.effect_key,
        now=NOW,
    )
    assert created
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into continuity_effect_binding values(?,?,?,?)",
            (claim.id, SESSION_ID, frozen.job_id, _binding().binding_digest),
        )
        db.commit()
    runtime.record_receipt(
        claim,
        status="unknown",
        evidence_digest=evidence,
        now="2026-09-03T12:01:00+00:00",
    )
    runtime.finish(work, state="recovery-required", now="2026-09-03T12:02:00+00:00")
    _unsafe_fixture_mutation(
        path,
        trigger_names=("local_recovery_case_guard_update",),
        statement="update local_recovery_case set created_at='not-a-time' where job_id=?",
        parameters=(frozen.job_id,),
    )

    with pytest.raises(PolicyViolation, match=r"recovery case.*timestamp"):
        writer.freeze_with_preclose(_request(seed))


def test_compile_outbox_claim_cannot_precede_completed_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "compile-delivery-before-job-completion.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=3600,
        now="2026-09-03T12:01:00+00:00",
    )
    assert work is not None
    evidence = frozen.compile_evidence(_binding())
    claim, created = runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=evidence,
        idempotency_key=frozen.effect_key,
        now="2026-09-03T12:02:00+00:00",
    )
    assert created
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into continuity_effect_binding values(?,?,?,?)",
            (claim.id, SESSION_ID, frozen.job_id, _binding().binding_digest),
        )
        db.commit()
    runtime.record_receipt(
        claim,
        status="completed",
        evidence_digest=evidence,
        now="2026-09-03T12:03:00+00:00",
    )
    runtime.finish(
        work,
        state="completed",
        evidence_digest=evidence,
        now="2026-09-03T12:04:00+00:00",
    )
    delivery = runtime.claim_outbox(
        supported_kinds=("continuity.compile",),
        outbox_id=frozen.outbox_id,
        require_completed_job=True,
        owner_id="delivery",
        owner_pid=102,
        owner_token="delivery-incarnation",
        lease_seconds=3600,
        now="2026-09-03T12:05:00+00:00",
    )
    assert delivery is not None
    with sqlite3.connect(path) as db:
        db.execute(
            "update local_outbox_delivery set updated_at='2026-09-03T12:01:00+00:00' "
            "where outbox_id=?",
            (frozen.outbox_id,),
        )
        db.commit()
    monkeypatch.setattr(
        SQLiteDormantV4CloseWriter,
        "_trusted_now",
        staticmethod(lambda: dt.datetime(2026, 9, 3, 12, 6, tzinfo=dt.UTC)),
    )

    with pytest.raises(PolicyViolation, match=r"delivery.*completed job"):
        writer.freeze_with_preclose(_request(seed))


def test_real_runtime_distinct_causal_timestamps_remain_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "runtime-distinct-causal-times.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=3600,
        now="2026-09-03T12:01:00+00:00",
    )
    assert work is not None
    evidence = frozen.compile_evidence(_binding())
    claim, created = runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=evidence,
        idempotency_key=frozen.effect_key,
        now="2026-09-03T12:02:00+00:00",
    )
    assert created
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into continuity_effect_binding values(?,?,?,?)",
            (claim.id, SESSION_ID, frozen.job_id, _binding().binding_digest),
        )
        db.commit()
    runtime.record_receipt(
        claim,
        status="completed",
        evidence_digest=evidence,
        now="2026-09-03T12:03:00+00:00",
    )
    runtime.finish(
        work,
        state="completed",
        evidence_digest=evidence,
        now="2026-09-03T12:04:00+00:00",
    )
    with sqlite3.connect(path) as db:
        outboxes = db.execute(
            "select id,event_kind from local_outbox where job_id=? order by id",
            (frozen.job_id,),
        ).fetchall()
    for outbox_id, kind in outboxes:
        delivery = runtime.claim_outbox(
            supported_kinds=(str(kind),),
            outbox_id=str(outbox_id),
            require_completed_job=True,
            owner_id="delivery",
            owner_pid=102,
            owner_token="delivery-incarnation",
            lease_seconds=3600,
            now="2026-09-03T12:05:00+00:00",
        )
        assert delivery is not None
        delivery_evidence = (
            frozen.delivery_evidence(_binding())
            if outbox_id == frozen.outbox_id
            else digest({"outbox_id": outbox_id, "delivered": True})
        )
        runtime.record_outbox_receipt(
            delivery,
            status="delivered",
            evidence_digest=delivery_evidence,
            now="2026-09-03T12:06:00+00:00",
        )

    assert writer.freeze_with_preclose(_request(seed)).state == "pending"


def _unknown_effect_recovery_graph(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    dict[str, object],
    SQLiteDormantV4CloseWriter,
    FrozenClose,
    SQLiteLocalRuntimeStore,
    str,
]:
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=3600,
        now=NOW,
    )
    assert work is not None
    evidence = frozen.compile_evidence(_binding())
    claim, created = runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=evidence,
        idempotency_key=frozen.effect_key,
        now=NOW,
    )
    assert created
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into continuity_effect_binding values(?,?,?,?)",
            (claim.id, SESSION_ID, frozen.job_id, _binding().binding_digest),
        )
        db.commit()
    runtime.record_receipt(
        claim,
        status="unknown",
        evidence_digest=evidence,
        now="2026-09-03T12:01:00+00:00",
    )
    runtime.finish(work, state="recovery-required", now="2026-09-03T12:02:00+00:00")
    return seed, writer, frozen, runtime, evidence


def test_unknown_effect_case_creation_equals_receipt_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "unknown-case-after-receipt.db"
    seed, writer, frozen, _, _ = _unknown_effect_recovery_graph(path, monkeypatch)
    _unsafe_fixture_mutation(
        path,
        trigger_names=("local_recovery_case_guard_update",),
        statement=(
            "update local_recovery_case set created_at='2026-09-03T12:01:30+00:00' where job_id=?"
        ),
        parameters=(frozen.job_id,),
    )

    with pytest.raises(PolicyViolation, match="effect recovery graph drift"):
        writer.freeze_with_preclose(_request(seed))


def test_effect_resolution_cannot_postdate_reconciled_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "effect-resolution-after-reconcile.db"
    seed, writer, frozen, runtime, evidence = _unknown_effect_recovery_graph(path, monkeypatch)
    with sqlite3.connect(path) as db:
        case_id = str(
            db.execute(
                "select id from local_recovery_case where job_id=?", (frozen.job_id,)
            ).fetchone()[0]
        )
    runtime.resolve_recovery(
        case_id,
        outcome="completed",
        evidence_digest=evidence,
        now="2026-09-03T12:03:00+00:00",
    )
    runtime.reconcile_recovery(frozen.job_id, now="2026-09-03T12:04:00+00:00")
    with sqlite3.connect(path) as db:
        trigger_names = (
            "local_recovery_case_guard_update",
            "local_recovery_resolution_no_update",
        )
        trigger_sql = [
            str(
                db.execute(
                    "select sql from sqlite_master where type='trigger' and name=?", (name,)
                ).fetchone()[0]
            )
            for name in trigger_names
        ]
        for name in trigger_names:
            db.execute(f'drop trigger "{name}"')
        db.execute(
            "update local_recovery_case set resolved_at='2026-09-03T14:00:00+00:00' where id=?",
            (case_id,),
        )
        db.execute(
            "update local_recovery_resolution set created_at='2026-09-03T14:00:00+00:00' "
            "where recovery_case_id=?",
            (case_id,),
        )
        for statement in trigger_sql:
            db.execute(statement)
        db.commit()
    assert operational_schema.status(path).schema_ok

    with pytest.raises(PolicyViolation, match=r"resolution.*job"):
        writer.freeze_with_preclose(_request(seed))


def test_unknown_effect_reconciliation_accepts_distinct_causal_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "effect-reconciliation-distinct-times.db"
    seed, writer, frozen, runtime, evidence = _unknown_effect_recovery_graph(path, monkeypatch)
    with sqlite3.connect(path) as db:
        case_id = str(
            db.execute(
                "select id from local_recovery_case where job_id=?", (frozen.job_id,)
            ).fetchone()[0]
        )
    runtime.resolve_recovery(
        case_id,
        outcome="completed",
        evidence_digest=evidence,
        now="2026-09-03T12:03:00+00:00",
    )
    runtime.reconcile_recovery(frozen.job_id, now="2026-09-03T12:04:00+00:00")

    assert writer.freeze_with_preclose(_request(seed)).state == "pending"


def test_resolved_outbox_case_creation_equals_unknown_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "resolved-outbox-case-time.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    _materialize_runtime_variant(
        path,
        frozen,
        monkeypatch,
        mode="direct",
        resolved_unknown_delivery=True,
    )
    _unsafe_fixture_mutation(
        path,
        trigger_names=("local_recovery_case_guard_update",),
        statement=(
            "update local_recovery_case set created_at='2026-09-03T12:00:01+00:00' "
            "where outbox_id is not null"
        ),
    )

    with pytest.raises(PolicyViolation, match="delivery recovery graph drift"):
        writer.freeze_with_preclose(_request(seed))


def test_expired_outbox_recovery_after_expiry_remains_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "expired-outbox-recovery.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    monkeypatch.setattr(runtime_module, "SCHEMA_VERSION", 4)
    runtime = SQLiteLocalRuntimeStore(path, existing_only=True)
    work = runtime.claim_next(
        supported_operations=("continuity.compile",),
        job_id=frozen.job_id,
        owner_id="worker",
        owner_pid=101,
        owner_token="incarnation",
        lease_seconds=3600,
        now="2026-09-03T12:01:00+00:00",
    )
    assert work is not None
    evidence = frozen.compile_evidence(_binding())
    claim, created = runtime.claim_effect(
        work,
        operation="continuity.compile",
        effect_digest=evidence,
        idempotency_key=frozen.effect_key,
        now="2026-09-03T12:02:00+00:00",
    )
    assert created
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into continuity_effect_binding values(?,?,?,?)",
            (claim.id, SESSION_ID, frozen.job_id, _binding().binding_digest),
        )
        db.commit()
    runtime.record_receipt(
        claim,
        status="completed",
        evidence_digest=evidence,
        now="2026-09-03T12:03:00+00:00",
    )
    runtime.finish(
        work,
        state="completed",
        evidence_digest=evidence,
        now="2026-09-03T12:04:00+00:00",
    )
    with sqlite3.connect(path) as db:
        outbox_id = str(
            db.execute(
                "select id from local_outbox where job_id=? and event_kind='job.enqueued'",
                (frozen.job_id,),
            ).fetchone()[0]
        )
    delivery = runtime.claim_outbox(
        supported_kinds=("job.enqueued",),
        outbox_id=outbox_id,
        require_completed_job=True,
        owner_id="delivery",
        owner_pid=102,
        owner_token="delivery-incarnation",
        lease_seconds=1,
        now="2026-09-03T12:05:00+00:00",
    )
    assert delivery is not None
    assert runtime.recover_outbox(now="2026-09-03T12:05:02+00:00") == 1

    assert writer.freeze_with_preclose(_request(seed)).state == "recovery-required"


def test_direct_unknown_outbox_receipt_must_precede_claim_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "direct-unknown-at-expiry.db"
    seed = _seed(path)
    writer = _v4_writer(path, seed)
    frozen = writer.freeze_with_preclose(_request(seed))
    _materialize_runtime_variant(
        path,
        frozen,
        monkeypatch,
        mode="direct",
        resolved_unknown_delivery=True,
    )
    with sqlite3.connect(path) as db:
        outbox_id = str(
            db.execute(
                "select outbox_id from local_recovery_case where outbox_id is not null"
            ).fetchone()[0]
        )
        db.execute(
            "update local_outbox_delivery set expires_at=? where outbox_id=?",
            (NOW, outbox_id),
        )
        db.commit()

    with pytest.raises(PolicyViolation, match="delivery recovery graph drift"):
        writer.freeze_with_preclose(_request(seed))
