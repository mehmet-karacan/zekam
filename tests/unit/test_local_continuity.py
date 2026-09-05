"""Local continuity durability and rejection tests using one real Akilli Kasa file."""

from __future__ import annotations

import datetime as dt
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.context_compiler import compile_context_v2
from zekam.application.context_ranking import ContextRankingRequest, count_context_tokens
from zekam.application.local_continuity import (
    ContinuityBinding,
    ContinuityEvent,
    ContinuityTail,
    LocalContext,
)
from zekam.application.local_continuity_service import LocalLifecycleContinuity
from zekam.application.local_runtime import RUNTIME_OUTBOX_KINDS
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
)
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.codex_lifecycle import parse_codex_hook_input
from zekam.infrastructure.local_continuity_source import ProjectContinuitySourceResolver
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

ROOT = Path("/Users/mkaracan/Projeler/akilli-kasa")
SOURCE_REF = "src/akilli_kasa/api/saglik.py"
NOW = dt.datetime(2026, 9, 2, 18, tzinfo=dt.UTC)


def _resolver(binding: ContinuityBinding) -> ProjectContinuitySourceResolver:
    return ProjectContinuitySourceResolver(
        ROOT,
        project_id=binding.project_id,
        realm_id=binding.realm_id,
        source_snapshot_id=binding.source_snapshot_id,
        allowed_paths=(SOURCE_REF,),
    )


@pytest.fixture
def continuity(tmp_path: Path) -> tuple[SQLiteContinuityStore, ContinuityBinding, LocalContext]:
    if not (ROOT / SOURCE_REF).is_file():
        pytest.skip("Bounded read-only Akilli Kasa source unavailable")
    text = (ROOT / SOURCE_REF).read_text()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    path = tmp_path / "operational.db"
    bootstrap(path)
    operational = SQLiteOperationalStore(path)
    with operational.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest({"network": "deny"}),
            task_digest=digest("wp08"),
            sanitized_config={"network": "deny"},
        )
        project = uow.create_project(slug="akilli-kasa", display_name="Akilli Kasa")
        source = uow.bind_source(
            project_id=project.id, portable_ref="project/akilli-kasa", source_kind="git"
        )
        snapshot = uow.capture_source_snapshot(
            source_binding_id=source.id,
            revision_ref=revision,
            tree_digest=digest(text),
            content_digest=digest(text),
            config_digest=digest("bounded-one-file"),
        )
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Continuity validation",
            state="ready",
            payload_digest=digest("work"),
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            source_snapshot_id=snapshot.id,
            plan_digest=digest("wp08-plan"),
            budget={"max_seconds": 60},
        )
        uow.commit()
    realm = str(uuid4())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "insert into project_knowledge_realm values(?,?,?)",
            (project.id, realm, NOW.isoformat()),
        )
    binding = ContinuityBinding(
        str(uuid4()),
        str(uuid4()),
        project.id,
        realm,
        "codex",
        "macbook",
        snapshot.id,
        digest("wp08"),
        digest("wp08-plan"),
        digest({"network": "deny"}),
        work.id,
        run.id,
    )
    store = SQLiteContinuityStore(path, source_resolver=_resolver(binding))
    store.bind_session(binding)
    candidate = ContextCandidate(
        candidate_id="health-source",
        authority=AuthorityLevel.VERIFIED,
        observed_at=NOW,
        source_revision=revision,
        content_digest=digest(text),
        token_count=count_context_tokens(text),
        required=True,
        kind=ContextCandidateKind.SOURCE_SLICE,
        source_ref=SOURCE_REF,
        identity_refs=("task/wp08",),
        scope_ref=f"project/{project.id}",
        applicable_roles=("builder",),
    )
    request = ContextRankingRequest(
        role="builder",
        target_identity_refs=("task/wp08",),
        step_scope_ref=None,
        work_scope_ref=f"work/{work.id}",
        project_scope_ref=f"project/{project.id}",
        realm_scope_ref=f"realm/{realm}",
        current_source_revision=revision,
        compatible_source_revisions=(),
        task_terms=(),
        tokenizer_profile_digest=DEFAULT_TOKENIZER_PROFILE_DIGEST,
    )
    manifest = compile_context_v2(
        (candidate,),
        ranking_request=request,
        token_budget=4096,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
        contents={"health-source": text},
        ranking_snapshot_digest=digest(request.body()),
        candidate_set_digest=digest(candidate.candidate_digest),
        recipe_id="continuity-v2",
        recipe_digest=digest("recipe"),
        target_role="builder",
    )
    return store, binding, LocalContext(manifest, (("health-source", text),), request, (candidate,))


def _event(key: str = "start", *, spool: str | None = None) -> ContinuityEvent:
    return ContinuityEvent("SESSION_START", key, NOW.isoformat(), (SOURCE_REF,), (), spool)


def _checkpoint(
    store: SQLiteContinuityStore, binding: ContinuityBinding, context: LocalContext
) -> str:
    manifest = store.hydrate(binding, context, idempotency_key="hydrate")
    tail = store.append_event(binding, _event(), expected_tail=ContinuityTail(0, None))
    return store.checkpoint(
        binding,
        expected_tail=tail,
        context_digest=manifest,
        idempotency_key="checkpoint",
        spool_digests=(),
    )


def test_real_source_context_and_checkpoint_survive_reopen_without_authority(
    continuity: Any,
) -> None:
    store, binding, context = continuity
    checkpoint = _checkpoint(store, binding, context)
    before = store.resume(binding, checkpoint)
    reopened = SQLiteContinuityStore(store.path, source_resolver=_resolver(binding))
    assert reopened.resume(binding, checkpoint) == before
    assert (
        before["context"]["context"]["fragments"]["health-source"]
        == (ROOT / SOURCE_REF).read_text()
    )
    assert before["grants_authority"] is before["approval_inherited"] is False
    assert before["reacquire_required"] is True
    assert before["uncovered_events"] == 0
    assert store.bind_session(binding) == binding.binding_digest
    assert _checkpoint(store, binding, context) == checkpoint


@pytest.mark.parametrize("value", [None, "", 1, True, {}, [], "../../outside", "/Users/private"])
def test_event_wrong_type_empty_and_unportable_keys_rejected(value: Any) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        ContinuityEvent("SESSION_START", value, NOW.isoformat())


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": None},
        {"kind": "PROMPT"},
        {"source_refs": None},
        {"source_refs": []},
        {"source_refs": (SOURCE_REF, SOURCE_REF)},
        {"evidence_digests": ("broken",)},
        {"occurred_at": None},
        {"occurred_at": "2026-09-02T18:00:00"},
        {"source_refs": tuple(f"source/{i}" for i in range(33))},
    ],
)
def test_event_validation_matrix(changes: Any) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        replace(_event(), **changes)


def test_event_duplicate_changed_payload_and_sequence_race(continuity: Any) -> None:
    store, binding, _ = continuity
    empty = ContinuityTail(0, None)
    first = store.append_event(binding, _event(), expected_tail=empty)
    assert store.append_event(binding, _event(), expected_tail=empty) == first
    with pytest.raises(PolicyViolation, match="replay"):
        store.append_event(binding, replace(_event(), kind="PRE_CLOSE"), expected_tail=empty)

    def append(index: int) -> bool:
        try:
            store.append_event(binding, _event(f"turn-{index}"), expected_tail=first)
            return True
        except ConcurrencyConflict:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(append, range(4))) == 1
    assert store.tail(binding).sequence == 2


def test_checkpoint_unpersisted_spool_delta_and_pending_outbox_block_ack(continuity: Any) -> None:
    store, binding, context = continuity
    manifest = store.hydrate(binding, context, idempotency_key="hydrate")
    first = digest("first-spool-entry")
    tail = store.append_event(binding, _event(spool=first), expected_tail=ContinuityTail(0, None))
    with pytest.raises(PolicyViolation, match="spool"):
        store.checkpoint(
            binding,
            expected_tail=tail,
            context_digest=manifest,
            idempotency_key="cp",
            spool_digests=(first, digest("unpersisted")),
        )
    runtime = SQLiteLocalRuntimeStore(store.path)
    runtime.enqueue(
        idempotency_key="pending",
        payload={
            "session_id": binding.session_id,
            "binding_digest": binding.binding_digest,
            "run_id": binding.run_id,
        },
    )
    with pytest.raises(PolicyViolation, match="outbox"):
        store.checkpoint(
            binding,
            expected_tail=tail,
            context_digest=manifest,
            idempotency_key="cp",
            spool_digests=(first,),
        )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 0


@pytest.mark.parametrize(
    "field",
    ["realm_id", "project_id", "run_id", "work_item_id", "device_id", "task_digest", "plan_digest"],
)
def test_binding_drift_cannot_read_or_append(continuity: Any, field: str) -> None:
    store, binding, context = continuity
    checkpoint = _checkpoint(store, binding, context)
    value = (
        digest("wrong")
        if field.endswith("digest")
        else "other-device"
        if field == "device_id"
        else str(uuid4())
    )
    wrong = replace(binding, **{field: value})
    with pytest.raises(PolicyViolation, match="binding"):
        store.resume(wrong, checkpoint)
    with pytest.raises(PolicyViolation, match="binding"):
        store.append_event(wrong, _event("wrong"), expected_tail=store.tail(binding))


def test_cross_owner_new_session_rejected_without_partial_rows(continuity: Any) -> None:
    store, binding, _ = continuity
    operational = SQLiteOperationalStore(store.path)
    with operational.unit_of_work() as uow:
        other = uow.create_project(slug="other", display_name="Other")
        uow.commit()
    wrong = replace(
        binding, session_id=str(uuid4()), external_session_id="other-session", project_id=other.id
    )
    with pytest.raises(ValidationFailed):
        store.bind_session(wrong)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("select count(*) from session").fetchone()[0] == 1


def test_context_modified_content_budget_wrong_type_and_extra_fragment_rejected(
    continuity: Any,
) -> None:
    _, _, context = continuity
    with pytest.raises(PolicyViolation, match="digest"):
        replace(context, fragments=(("health-source", "modified"),))
    with pytest.raises(PolicyViolation, match="partition"):
        replace(context, fragments=(*context.fragments, ("extra", "not selected")))
    with pytest.raises(ValidationFailed, match="bounds"):
        replace(
            context,
            manifest=replace(context.manifest, token_budget=True, selected=()),
            fragments=(),
        )


def test_source_revision_drift_blocks_resume(continuity: Any) -> None:
    store, binding, context = continuity
    checkpoint = _checkpoint(store, binding, context)
    with sqlite3.connect(store.path) as connection:
        source_id = connection.execute(
            "select source_binding_id from source_snapshot where id=?",
            (binding.source_snapshot_id,),
        ).fetchone()[0]
    with SQLiteOperationalStore(store.path).unit_of_work() as uow:
        uow.capture_source_snapshot(
            source_binding_id=source_id,
            revision_ref="new-revision",
            tree_digest=digest("new"),
            content_digest=digest("new"),
            config_digest=digest("new"),
        )
        uow.commit()
    with pytest.raises(PolicyViolation, match="stale"):
        store.resume(binding, checkpoint)


def test_raw_unpaired_event_is_recovery_gap_not_silently_covered(continuity: Any) -> None:
    store, binding, _ = continuity
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "insert into session_event values(?,?,?,?,?)",
            (str(uuid4()), binding.session_id, "unpaired", digest("unpaired"), NOW.isoformat()),
        )
    with pytest.raises(PolicyViolation, match="gap"):
        store.tail(binding)


def test_checkpoint_and_event_evidence_append_only(continuity: Any) -> None:
    store, binding, context = continuity
    _checkpoint(store, binding, context)
    with sqlite3.connect(store.path) as connection:
        for table in (
            "session_event_detail",
            "continuity_checkpoint",
            "context_manifest",
            "hydration_receipt",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(f"delete from {table}")


def test_canonical_session_owner_guard_and_runtime_revalidation(continuity: Any) -> None:
    store, binding, _ = continuity
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="owner immutable"):
            connection.execute("update session set device_id='other-device'")
        # Simulated damaged protection: the existing adapter must still reject ownership drift.
        connection.execute("drop trigger continuity_session_owner_update_guard")
        connection.execute("update session set device_id='other-device'")
    with pytest.raises(PolicyViolation, match="session owner drift"):
        store.tail(binding)


def _stage(spool: ClientLifecycleSpool, binding: ContinuityBinding, kind: str) -> Any:
    document = {
        "session_id": binding.external_session_id,
        "hook_event_name": kind,
    }
    if kind == "PreCompact":
        document["trigger"] = "manual"
        document["turn_id"] = str(uuid4())
    else:
        document["source"] = "startup"
        document["permission_mode"] = "default"
    parsed = parse_codex_hook_input(json.dumps(document))
    return spool.stage(
        parsed.observation_body(),
        delivery_id=parsed.delivery_id(occurrence_id=str(uuid4())),
        occurred_at=NOW,
    )


def test_real_spool_missing_delta_blocks_ack_then_drains_and_resumes(
    continuity: Any, tmp_path: Path
) -> None:
    store, binding, context = continuity
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    service = LocalLifecycleContinuity(
        store, spool, binding, source_probe=lambda: digest((ROOT / SOURCE_REF).read_text())
    )
    manifest = service.hydrate(context, key="start")
    with pytest.raises(PolicyViolation, match="hook evidence missing"):
        service.pre_compaction(context_digest=manifest, key="cp")
    _stage(spool, binding, "SessionStart")
    service.drain()
    _stage(spool, binding, "PreCompact")
    with pytest.raises(PolicyViolation, match="spool"):
        service.pre_compaction(context_digest=manifest, key="cp")
    assert service.drain() == 2
    checkpoint = service.pre_compaction(context_digest=manifest, key="cp")
    assert (
        SQLiteContinuityStore(store.path, source_resolver=_resolver(binding)).resume(
            binding, checkpoint
        )["uncovered_events"]
        == 0
    )
    assert service.drain() == 2
    assert service.pre_compaction(context_digest=manifest, key="cp") == checkpoint


def test_actual_source_probe_drift_or_failure_never_acknowledges(
    continuity: Any, tmp_path: Path
) -> None:
    store, binding, context = continuity
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    service = LocalLifecycleContinuity(
        store, spool, binding, source_probe=lambda: digest("changed")
    )
    with pytest.raises(PolicyViolation, match="actual source"):
        service.hydrate(context, key="start")

    def unavailable() -> str:
        raise TimeoutError("source probe unavailable")

    service.source_probe = unavailable
    with pytest.raises(TimeoutError):
        service.hydrate(context, key="start")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("select count(*) from hydration_receipt").fetchone()[0] == 0


def test_spool_writer_cannot_race_checkpoint_barrier(continuity: Any, tmp_path: Path) -> None:
    _, binding, _ = continuity
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    first = _stage(spool, binding, "SessionStart")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with spool.frozen_session_entries(
            client_id=binding.client_id, session_id=binding.external_session_id
        ) as entries:
            assert tuple(item.entry_digest for item in entries) == (first.entry_digest,)
            future = pool.submit(_stage, spool, binding, "PreCompact")
            with pytest.raises(TimeoutError):
                future.result(timeout=0.05)
        assert future.result(timeout=5).sequence == 2


def _child_resume(path: str, binding: dict[str, Any], checkpoint: str, queue: Any) -> None:
    identity = ContinuityBinding(**binding)
    result = SQLiteContinuityStore(Path(path), source_resolver=_resolver(identity)).resume(
        identity, checkpoint
    )
    queue.put(digest(result))


def test_new_process_restores_exact_context_bytes(continuity: Any) -> None:
    store, binding, context = continuity
    checkpoint = _checkpoint(store, binding, context)
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    process = ctx.Process(
        target=_child_resume, args=(str(store.path), asdict(binding), checkpoint, queue)
    )
    process.start()
    assert queue.get(timeout=10) == digest(store.resume(binding, checkpoint))
    process.join(timeout=10)
    assert process.exitcode == 0


def _child_checkpoint_kill(path: str, binding_data: dict[str, Any], manifest: str) -> None:
    binding = ContinuityBinding(**binding_data)
    store = SQLiteContinuityStore(Path(path), source_resolver=_resolver(binding))
    tail = store.tail(binding)
    transaction = store._transaction

    @contextmanager
    def killed_transaction() -> Any:
        with transaction() as connection:
            yield connection
            os._exit(91)

    store._transaction = killed_transaction  # type: ignore[method-assign]
    store.checkpoint(
        binding,
        expected_tail=tail,
        context_digest=manifest,
        idempotency_key="killed",
        spool_digests=(),
    )


def test_process_death_before_checkpoint_commit_has_no_partial_ack(continuity: Any) -> None:
    store, binding, context = continuity
    manifest = store.hydrate(binding, context, idempotency_key="hydrate")
    store.append_event(binding, _event(), expected_tail=ContinuityTail(0, None))
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(
        target=_child_checkpoint_kill, args=(str(store.path), asdict(binding), manifest)
    )
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 91
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 0
    recovered = SQLiteContinuityStore(store.path, source_resolver=_resolver(binding))
    assert recovered.checkpoint(
        binding,
        expected_tail=recovered.tail(binding),
        context_digest=manifest,
        idempotency_key="killed",
        spool_digests=(),
    )


def test_duplicate_manifest_selection_rejected_before_storage(continuity: Any) -> None:
    _, _, context = continuity
    manifest = context.manifest
    with pytest.raises(PolicyViolation, match="duplicate"):
        replace(context, manifest=replace(manifest, selected=manifest.selected * 2))


@pytest.mark.parametrize(
    "column,value", [("spool_digest", digest("forged")), ("idempotency_key", "changed")]
)
def test_event_columns_cannot_diverge_from_immutable_body(
    continuity: Any, column: str, value: str
) -> None:
    store, binding, _ = continuity
    store.append_event(binding, _event(), expected_tail=ContinuityTail(0, None))
    with sqlite3.connect(store.path) as connection:
        connection.execute("drop trigger session_event_detail_immutable_update")
        connection.execute(f"update session_event_detail set {column}=?", (value,))
    with pytest.raises(PolicyViolation, match="integrity"):
        store.tail(binding)


def test_cross_project_context_same_revision_and_relabelled_source_rejected(
    continuity: Any,
) -> None:
    store, binding, context = continuity
    original = context.selected_provenance[0]
    text = dict(context.fragments)[original.candidate_id]
    foreign = replace(original, scope_ref=f"project/{uuid4()}", source_ref="foreign/private.md")
    request = replace(
        context.ranking_request,
        project_scope_ref=foreign.scope_ref,
        realm_scope_ref=f"realm/{uuid4()}",
        work_scope_ref=f"work/{uuid4()}",
    )

    def compiled(candidate: ContextCandidate, ranking: ContextRankingRequest) -> LocalContext:
        manifest = compile_context_v2(
            (candidate,),
            ranking_request=ranking,
            token_budget=4096,
            minimum_authority=AuthorityLevel.OBSERVED,
            now=NOW,
            contents={candidate.candidate_id: text},
            ranking_snapshot_digest=digest(ranking.body()),
            candidate_set_digest=digest(candidate.candidate_digest),
            recipe_id="continuity-v2",
            recipe_digest=digest("recipe"),
            target_role="builder",
        )
        return LocalContext(manifest, ((candidate.candidate_id, text),), ranking, (candidate,))

    with pytest.raises(PolicyViolation, match="scope"):
        store.hydrate(binding, compiled(foreign, request), idempotency_key="foreign")
    # A local label must not turn a foreign reference into a proven source.
    relabelled = replace(foreign, scope_ref=original.scope_ref)
    with pytest.raises(PolicyViolation, match="approved bounded corpus"):
        store.hydrate(
            binding, compiled(relabelled, context.ranking_request), idempotency_key="relabelled"
        )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("select count(*) from hydration_receipt").fetchone()[0] == 0


def test_forged_checkpoint_body_cannot_claim_uncovered_events(continuity: Any) -> None:
    store, binding, context = continuity
    checkpoint = _checkpoint(store, binding, context)
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "select * from continuity_checkpoint where checkpoint_digest=?", (checkpoint,)
            ).fetchone()
        )
        body = json.loads(row["body_json"])
        body.update(covered_sequence=99, idempotency_key="forged")
        row.update(
            checkpoint_digest=digest(body), idempotency_key="forged", body_json=json.dumps(body)
        )
        connection.execute(
            "insert into continuity_checkpoint values(?,?,?,?,?,?,?,?,?,?)", tuple(row.values())
        )
    with pytest.raises(PolicyViolation, match="integrity"):
        store.resume(binding, digest(body))


def test_terminal_effect_recovery_unblocks_continuity_without_overwriting_unknown(
    continuity: Any,
) -> None:
    store, binding, context = continuity
    checkpoint = _checkpoint(store, binding, context)
    runtime = SQLiteLocalRuntimeStore(store.path)
    runtime.enqueue(
        idempotency_key="recoverable",
        payload={
            "session_id": binding.session_id,
            "binding_digest": binding.binding_digest,
            "run_id": binding.run_id,
        },
    )
    work = runtime.claim_next(
        owner_id="worker", owner_pid=1, owner_token="test-owner", lease_seconds=30
    )
    assert work is not None
    claim, _ = runtime.claim_effect(
        work,
        operation="test-effect",
        effect_digest=digest("effect"),
        idempotency_key="recoverable-effect",
    )
    store.bind_effect(binding, claim.id)
    runtime.record_receipt(claim, status="unknown", evidence_digest=digest("timeout"))
    runtime.finish(work, state="recovery-required")
    with pytest.raises(PolicyViolation, match="pending"):
        store.resume(binding, checkpoint)
    case = runtime.recovery_cases()[0]
    runtime.resolve_recovery(
        case.id, outcome="completed", evidence_digest=digest("verified-outcome")
    )
    assert runtime.reconcile_recovery(work.job.id).state == "completed"
    while (
        delivery := runtime.claim_outbox(
            supported_kinds=RUNTIME_OUTBOX_KINDS,
            owner_id="consumer",
            owner_pid=1,
            owner_token="test-consumer",
            lease_seconds=30,
        )
    ) is not None:
        runtime.record_outbox_receipt(
            delivery, status="delivered", evidence_digest=digest(delivery.event.id)
        )
    assert store.resume(binding, checkpoint)["uncovered_events"] == 0
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                "select status from local_effect_receipt where claim_id=?", (claim.id,)
            ).fetchone()[0]
            == "unknown"
        )


def test_doctor_is_read_only_and_exact_backfill_reports_missing_delta(
    continuity: Any, tmp_path: Path
) -> None:
    store, binding, context = continuity
    spool = ClientLifecycleSpool(tmp_path / "home", client_id="codex")
    service = LocalLifecycleContinuity(
        store, spool, binding, source_probe=lambda: digest((ROOT / SOURCE_REF).read_text())
    )
    before = store.path.read_bytes()
    report = service.doctor()
    assert "missing-required-hook-events" in report["issues"]
    assert not spool.root.exists()
    assert store.path.read_bytes() == before
    manifest = service.hydrate(context, key="start")
    _stage(spool, binding, "SessionStart")
    _stage(spool, binding, "PreCompact")
    assert "unpersisted-spool-delta" in service.doctor()["issues"]
    service.drain()
    service.pre_compaction(context_digest=manifest, key="cp")
    assert service.doctor()["state"] == "healthy"


def test_real_cli_inspection_preserves_db_and_does_not_claim_live_hook_acceptance(
    continuity: Any,
) -> None:
    store, binding, context = continuity
    _checkpoint(store, binding, context)
    before = store.path.read_bytes()
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "zekam.interfaces.cli.main",
            "continuity",
            "inspect",
            binding.session_id,
            "--database",
            str(store.path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["session_id"] == binding.session_id
    assert report["verification_scope"] == "operational-db-only"
    assert report["global_acceptance"] is report["filesystem_source_verified"] is False
    assert report["hook_activation_verified"] is False
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("locator", ["L0-L1", "L2-L1", "L1-L999999", "unexpected", ""])
def test_source_locator_boundaries_reject(continuity: Any, locator: str) -> None:
    _, binding, context = continuity
    source = context.selected_provenance[0].provenance_body | {
        "source_ref": f"{SOURCE_REF}#{locator}"
    }
    with pytest.raises(ValidationFailed, match="locator"):
        _resolver(binding)(binding, source)


def test_source_locator_resolves_exact_actual_lines_and_rejects_content_tamper(
    continuity: Any,
) -> None:
    _, binding, context = continuity
    lines = "".join((ROOT / SOURCE_REF).read_text().splitlines(keepends=True)[:2])
    source = context.selected_provenance[0].provenance_body | {
        "source_ref": f"{SOURCE_REF}#L1-L2",
        "digest": digest(lines),
    }
    assert _resolver(binding)(binding, source) == lines
    with pytest.raises(PolicyViolation, match="content digest mismatch"):
        _resolver(binding)(binding, source | {"digest": digest(lines + "changed")})


@pytest.mark.parametrize(
    "change",
    [
        {"kind": ContextCandidateKind.SYSTEM_POLICY},
        {"authority": AuthorityLevel.CANONICAL},
    ],
)
def test_context_selection_cannot_change_source_trust_metadata(
    continuity: Any, change: dict[str, Any]
) -> None:
    store, _, context = continuity
    selection = replace(context.manifest.selected[0], **change)
    with pytest.raises(PolicyViolation, match="provenance drift"):
        replace(context, manifest=replace(context.manifest, selected=(selection,)))
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("select count(*) from hydration_receipt").fetchone()[0] == 0


def test_unbound_step_scope_rejected_before_hydration(continuity: Any) -> None:
    store, binding, context = continuity
    ranking = replace(context.ranking_request, step_scope_ref="step/unowned")
    context = replace(
        context,
        ranking_request=ranking,
        manifest=replace(context.manifest, ranking_snapshot_digest=digest(ranking.body())),
    )
    with pytest.raises(PolicyViolation, match="scope mismatch"):
        store.hydrate(binding, context, idempotency_key="wrong-step")


@pytest.mark.parametrize("with_receipt", [False, True])
def test_hydration_replay_verifies_existing_manifest_columns(
    continuity: Any, with_receipt: bool
) -> None:
    store, binding, context = continuity
    body = {
        "binding_digest": binding.binding_digest,
        "session_id": binding.session_id,
        "checkpoint_digest": None,
        "context": context.body(),
    }
    manifest = digest(body)
    receipt = digest(
        {
            "session_id": binding.session_id,
            "manifest_digest": manifest,
            "idempotency_key": "hydrate",
            "grants_authority": False,
        }
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute(
            "insert into context_manifest values(?,?,?,?,?,?,?)",
            (
                manifest,
                binding.session_id,
                None,
                context.manifest.token_budget,
                0,
                canonical_json(body),
                NOW.isoformat(),
            ),
        )
        if with_receipt:
            connection.execute(
                "insert into hydration_receipt values(?,?,?,?,?)",
                (receipt, binding.session_id, manifest, "hydrate", NOW.isoformat()),
            )
    with pytest.raises(PolicyViolation, match="integrity"):
        store.hydrate(binding, context, idempotency_key="hydrate")


def test_checkpoint_replay_verifies_stored_body_before_ack(continuity: Any) -> None:
    store, binding, context = continuity
    manifest = store.hydrate(binding, context, idempotency_key="hydrate")
    tail = store.append_event(binding, _event(), expected_tail=ContinuityTail(0, None))
    body = {
        "session_id": binding.session_id,
        "binding_digest": binding.binding_digest,
        "covered_sequence": tail.sequence,
        "covered_event_digest": tail.event_digest,
        "source_snapshot_id": binding.source_snapshot_id,
        "context_digest": manifest,
        "spool_digest": digest(()),
        "idempotency_key": "checkpoint",
        "grants_authority": False,
        "approval_inherited": False,
    }
    expected = digest(body)
    with sqlite3.connect(store.path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute(
            "insert into continuity_checkpoint values(?,?,?,?,?,?,?,?,?,?)",
            (
                expected,
                binding.session_id,
                "checkpoint",
                tail.sequence,
                tail.event_digest,
                binding.source_snapshot_id,
                manifest,
                digest(()),
                canonical_json(body | {"covered_sequence": 99}),
                NOW.isoformat(),
            ),
        )
    with pytest.raises(PolicyViolation, match="integrity"):
        store.checkpoint(
            binding,
            expected_tail=tail,
            context_digest=manifest,
            idempotency_key="checkpoint",
            spool_digests=(),
        )
