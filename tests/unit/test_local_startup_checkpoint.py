"""Independent metadata-only predecessor evidence; real corpus is always read-only."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity_startup import NOW, ROOT, SOURCE_REF, _request, _stage_start
from tests.unit.test_local_continuity_startup import startup as startup
from tests.unit.test_local_startup_retrieval_integration import _checkpoint

from zekam.application.context_compiler import compile_context_v2
from zekam.application.context_ranking import ContextRankingRequest
from zekam.application.local_continuity import ContinuityEvent, LocalContext
from zekam.application.local_continuity_service import LocalLifecycleContinuity
from zekam.application.local_continuity_startup import LocalStartupService
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidateKind,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.local_continuity_decoder import validate_reviewed_control_entry
from zekam.infrastructure.local_continuity_source import ProjectContinuitySourceResolver
from zekam.infrastructure.sqlite.local_continuity_startup import SQLiteStartupSourceResolver
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.local_startup_checkpoint import SQLiteStartupCheckpointSource
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest


@pytest.fixture
def history(startup: dict[str, Any]) -> dict[str, Any]:
    result = startup["service"].hydrate(_request())
    checkpoint = _checkpoint(startup, result["manifest_digest"])
    binding = replace(startup["binding"], session_id=str(uuid4()), external_session_id=str(uuid4()))
    startup["base"].bind_session(binding)
    return startup | {
        "old": startup,
        "binding": binding,
        "checkpoint": checkpoint,
        "old_manifest": result["manifest_digest"],
        "checkpoints": SQLiteStartupCheckpointSource(startup["base"]),
    }


def _snapshot(value: dict[str, Any]) -> Any:
    return value["checkpoints"].snapshot(value["binding"], observed_at=NOW)


def _row(value: dict[str, Any], table: str, key: str, identity: str) -> dict[str, Any]:
    assert table in {"continuity_checkpoint", "context_manifest", "hydration_receipt"}
    assert key in {"checkpoint_digest", "manifest_digest"}
    with sqlite3.connect(value["path"]) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute(f"select * from {table} where {key}=?", (identity,)).fetchone())


def _rewrite_checkpoint(value: dict[str, Any], changes: dict[str, Any]) -> None:
    row = _row(value, "continuity_checkpoint", "checkpoint_digest", value["checkpoint"])
    body = json.loads(row["body_json"]) | changes
    changed = digest(body)
    fields = {key: val for key, val in changes.items() if key in row}
    fields |= {"body_json": canonical_json(body), "checkpoint_digest": changed}
    with sqlite3.connect(value["path"]) as db:
        db.execute("drop trigger if exists continuity_checkpoint_immutable_update")
        db.execute(
            "update continuity_checkpoint set "
            + ",".join(f"{key}=?" for key in fields)
            + " where checkpoint_digest=?",
            (*fields.values(), value["checkpoint"]),
        )
    value["checkpoint"] = changed


def _rewrite_manifest(value: dict[str, Any], change: Callable[[dict[str, Any]], None]) -> None:
    row = _row(value, "context_manifest", "manifest_digest", value["old_manifest"])
    body = json.loads(row["body_json"])
    change(body)
    changed = digest(body)
    with sqlite3.connect(value["path"]) as db:
        db.row_factory = sqlite3.Row
        db.execute("drop trigger context_manifest_immutable_update")
        db.execute("drop trigger hydration_receipt_immutable_update")
        db.execute(
            "update context_manifest set manifest_digest=?,body_json=? where manifest_digest=?",
            (changed, canonical_json(body), value["old_manifest"]),
        )
        for receipt in db.execute(
            "select * from hydration_receipt where manifest_digest=?", (value["old_manifest"],)
        ).fetchall():
            receipt_digest = digest(
                {
                    "session_id": receipt["session_id"],
                    "manifest_digest": changed,
                    "idempotency_key": receipt["idempotency_key"],
                    "grants_authority": False,
                }
            )
            db.execute(
                "update hydration_receipt set receipt_digest=?,manifest_digest=?"
                " where receipt_digest=?",
                (receipt_digest, changed, receipt["receipt_digest"]),
            )
    value["old_manifest"] = changed
    _rewrite_checkpoint(value, {"context_digest": changed})


def test_compatible_checkpoint_is_bounded_metadata_without_authority_or_old_text(
    history: dict[str, Any],
) -> None:
    before = logical_database_digest(history["path"])
    selected, report = _snapshot(history)
    candidate, text = selected
    body = json.loads(text)
    assert report["state"] == "compatible-metadata-only"
    assert candidate.kind is ContextCandidateKind.CHECKPOINT
    assert candidate.authority is AuthorityLevel.VERIFIED
    assert candidate.required is False
    assert candidate.scope_ref == f"work/{history['binding'].work_item_id}"
    assert candidate.canonical_revision_id == history["old"]["binding"].session_id
    assert candidate.source_revision == body["checkpoint_digest"] == history["checkpoint"]
    assert body["manifest_digest"] == history["old_manifest"]
    assert body["historical_evidence_only"] is body["reacquire_required"] is True
    assert body["grants_authority"] is body["approval_inherited"] is False
    assert SOURCE_REF in body["source_refs"]
    assert len(body["source_refs"]) <= 16
    assert body["source_ref_count"] == len(body["source_refs"]) + body["omitted_source_ref_count"]
    assert len(text.encode()) <= 16384
    assert history["text"] not in text and "class SaglikYaniti" not in text
    assert history["checkpoints"](history["binding"], candidate.provenance_body) == text
    assert logical_database_digest(history["path"]) == before


def test_self_checkpoint_is_not_a_predecessor_and_fresh_empty_is_truthful(
    history: dict[str, Any],
) -> None:
    selected, report = history["checkpoints"].snapshot(history["old"]["binding"], observed_at=NOW)
    assert selected is None and report["state"] == "fresh-empty"
    assert report["fragment_count"] == 0


def test_historical_read_never_reloads_spool_source_index_or_resolver(
    history: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Historical metadata must not activate old source/context/spool")

    monkeypatch.setattr(history["base"], "source_resolver", forbidden)
    monkeypatch.setattr(history["spool"], "frozen_session_entries", forbidden)
    monkeypatch.setattr(history["old"]["sources"], "assert_current", forbidden)
    selected, _ = _snapshot(history)
    candidate, text = selected
    assert history["checkpoints"](history["binding"], candidate.provenance_body) == text


@pytest.mark.parametrize("value", [None, {}, "binding", True])
def test_binding_strict_types(history: dict[str, Any], value: Any) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        history["checkpoints"].snapshot(value, observed_at=NOW)


@pytest.mark.parametrize("value", [None, "2026-09-02", NOW.replace(tzinfo=None), True])
def test_observation_strict_types(history: dict[str, Any], value: Any) -> None:
    with pytest.raises(ValidationFailed):
        history["checkpoints"].snapshot(history["binding"], observed_at=value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope_ref", "work/foreign"),
        ("canonical_revision_id", str(uuid4())),
        ("source_ref", "checkpoint/foreign"),
        ("revision", digest("foreign")),
        ("authority", 3),
        ("authority", True),
        ("tokens", True),
        ("digest", digest("foreign")),
        ("kind", "system-policy"),
        ("evidence_refs", [{"kind": "citation", "ref": "foreign", "digest": digest("other")}]),
    ],
)
def test_caller_cannot_relabel_historical_evidence(
    history: dict[str, Any], field: str, value: Any
) -> None:
    (candidate, _), _ = _snapshot(history)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        history["checkpoints"](history["binding"], candidate.provenance_body | {field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"covered_sequence": True},
        {"covered_sequence": 10001},
        {"covered_sequence": 2},
        {"covered_event_digest": digest("other-event")},
        {"spool_digest": digest(())},
        {"source_snapshot_id": str(uuid4())},
        {"binding_digest": digest("foreign")},
        {"grants_authority": True},
        {"approval_inherited": True},
        {"unexpected": "field"},
    ],
)
def test_rehashed_checkpoint_still_requires_exact_prefix_and_scope(
    history: dict[str, Any], changes: dict[str, Any]
) -> None:
    _rewrite_checkpoint(history, changes)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _snapshot(history)


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("context_manifest", "token_count", 1),
        ("context_manifest", "body_json", "{}"),
        ("hydration_receipt", "receipt_digest", digest("wrong-receipt")),
        ("hydration_receipt", "session_id", str(uuid4())),
        ("continuity_checkpoint", "created_at", "bad-time"),
        ("continuity_checkpoint", "covered_sequence", 2),
        ("continuity_checkpoint", "body_json", "{}"),
    ],
)
def test_immutable_rows_reject_updates_and_runtime_rejects_corrupt_restore(
    history: dict[str, Any],
    table: str,
    column: str,
    value: Any,
) -> None:
    with sqlite3.connect(history["path"]) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(f"update {table} set {column}=?", (value,))
        db.execute(f"drop trigger {table}_immutable_update")
        db.execute(f"update {table} set {column}=?", (value,))
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _snapshot(history)


def test_missing_hydration_receipt_is_not_treated_as_complete_history(
    history: dict[str, Any],
) -> None:
    with sqlite3.connect(history["path"]) as db:
        db.execute("drop trigger hydration_receipt_immutable_delete")
        db.execute("delete from hydration_receipt")
    with pytest.raises(PolicyViolation, match="receipt missing"):
        _snapshot(history)


@pytest.mark.parametrize(
    "variant", ["fragment", "duplicate", "scope", "grant", "metric-bool", "missing-fragment"]
)
def test_rehashed_manifest_is_structurally_checked_not_merely_hash_checked(
    history: dict[str, Any], variant: str
) -> None:
    def change(body: dict[str, Any]) -> None:
        context = body["context"]
        if variant == "fragment":
            context["fragments"][next(iter(context["fragments"]))] = "forged"
        elif variant == "duplicate":
            context["compiler"]["selected"].append(context["compiler"]["selected"][0])
        elif variant == "scope":
            source = context["selected_provenance"][0]
            source["scope_ref"] = "project/foreign"
            for item in context["compiler"]["selected"]:
                if item["candidate_id"] == source["id"]:
                    item["candidate_digest"] = digest(source)
        elif variant == "grant":
            context["grants_authority"] = True
        elif variant == "metric-bool":
            context["compiler"]["compiler_metrics"]["omitted_count"] = False
        else:
            context["fragments"].pop(next(iter(context["fragments"])))

    _rewrite_manifest(history, change)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _snapshot(history)


@pytest.mark.parametrize(
    "variant", ["canonical-owner", "event-digest", "detail-body", "canonical-only"]
)
def test_owner_and_event_ledger_corruption_rejects_history(
    history: dict[str, Any], variant: str
) -> None:
    old = history["old"]["binding"]
    with sqlite3.connect(history["path"]) as db:
        if variant == "canonical-owner":
            db.execute("drop trigger continuity_session_owner_update_guard")
            db.execute("update session set device_id='foreign' where id=?", (old.session_id,))
        elif variant == "event-digest":
            db.execute("drop trigger session_event_detail_immutable_update")
            db.execute("update session_event_detail set event_digest=?", (digest("wrong-event"),))
        elif variant == "detail-body":
            db.execute("drop trigger session_event_detail_immutable_update")
            db.execute("update session_event_detail set body_json='{}'")
        else:
            db.execute("drop trigger session_event_detail_immutable_delete")
            db.execute("delete from session_event_detail")
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _snapshot(history)


def test_future_events_are_not_claimed_by_old_checkpoint(history: dict[str, Any]) -> None:
    (candidate, first), _ = _snapshot(history)
    base, old = history["base"], history["old"]["binding"]
    base.append_event(
        old,
        ContinuityEvent("USER_TURN_COMMITTED", "future-turn", NOW.isoformat()),
        expected_tail=base.tail(old),
    )
    assert history["checkpoints"](history["binding"], candidate.provenance_body) == first
    assert json.loads(first)["covered_sequence"] == 1
    assert base.tail(old).sequence == 2


def test_new_checkpoint_does_not_replace_existing_pinned_candidate(history: dict[str, Any]) -> None:
    (candidate, first), _ = _snapshot(history)
    old, base = history["old"], history["base"]
    next_digest = base.checkpoint(
        old["binding"],
        expected_tail=base.tail(old["binding"]),
        context_digest=history["old_manifest"],
        idempotency_key="second-checkpoint",
        spool_digests=base.spool_digests(old["binding"]),
    )
    newest, report = _snapshot(history)
    assert newest[0].source_revision == report["checkpoint_digest"] == next_digest
    assert history["checkpoints"](history["binding"], candidate.provenance_body) == first


def test_provider_free_current_source_remains_unmodified(history: dict[str, Any]) -> None:
    before = (ROOT / SOURCE_REF).read_bytes()
    _snapshot(history)
    assert (ROOT / SOURCE_REF).read_bytes() == before


def _compose_new(value: dict[str, Any]) -> dict[str, Any]:
    binding, base = value["binding"], value["base"]
    project = ProjectContinuitySourceResolver(
        ROOT,
        project_id=binding.project_id,
        realm_id=binding.realm_id,
        source_snapshot_id=binding.source_snapshot_id,
        allowed_paths=(SOURCE_REF,),
    )
    sources = SQLiteStartupSourceResolver(base, project, checkpoints=value["checkpoints"])
    base.source_resolver = sources
    lifecycle = LocalLifecycleContinuity(
        base,
        value["spool"],
        binding,
        source_probe=lambda: digest((ROOT / SOURCE_REF).read_text()),
        entry_validator=validate_reviewed_control_entry,
    )
    result = value | {
        "lifecycle": lifecycle,
        "sources": sources,
        "service": LocalStartupService(lifecycle, sources),
    }
    _stage_start(result)
    return result


def _new_receipts(value: dict[str, Any]) -> int:
    with sqlite3.connect(value["path"]) as db:
        return int(
            db.execute(
                "select count(*) from hydration_receipt where session_id=?",
                (value["binding"].session_id,),
            ).fetchone()[0]
        )


def test_new_session_hydrates_only_checkpoint_metadata_then_real_process_resume(
    history: dict[str, Any],
) -> None:
    value = _compose_new(history)
    result = value["service"].hydrate(_request(idempotency_key="new-startup"))
    assert result["prior_checkpoint"]["state"] == "compatible-metadata-only"
    assert "prior-checkpoint" not in result["remaining_gates"]
    manifest = _row(value, "context_manifest", "manifest_digest", result["manifest_digest"])
    context = json.loads(manifest["body_json"])["context"]
    checkpoints = [c for c in context["selected_provenance"] if c["kind"] == "checkpoint"]
    assert len(checkpoints) == 1
    checkpoint_metadata = context["fragments"][checkpoints[0]["id"]]
    assert history["text"] not in checkpoint_metadata
    assert json.loads(checkpoint_metadata)["checkpoint_digest"] == history["checkpoint"]
    assert _new_receipts(value) == 1
    current_cp = _checkpoint(value, result["manifest_digest"])
    expected = value["base"].resume(value["binding"], current_cp)
    script = """
import json, socket, sys
from pathlib import Path
from zekam.application.local_continuity import ContinuityBinding
from zekam.infrastructure.local_continuity_source import ProjectContinuitySourceResolver
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_startup import SQLiteStartupSourceResolver
from zekam.infrastructure.sqlite.local_startup_checkpoint import SQLiteStartupCheckpointSource
def forbidden(*args, **kwargs): raise AssertionError('No provider/network on resume')
socket.socket.connect = forbidden
socket.create_connection = forbidden
r = json.load(sys.stdin)
b = ContinuityBinding(**r['binding'])
base = SQLiteContinuityStore(Path(r['path']))
p = ProjectContinuitySourceResolver(Path(r['root']), project_id=b.project_id, realm_id=b.realm_id,
    source_snapshot_id=b.source_snapshot_id, allowed_paths=(r['source_ref'],))
base.source_resolver = SQLiteStartupSourceResolver(base, p,
    checkpoints=SQLiteStartupCheckpointSource(base))
print(json.dumps(base.resume(b, r['checkpoint']), sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
        input=json.dumps(
            {
                "binding": asdict(value["binding"]),
                "path": str(value["path"]),
                "root": str(ROOT),
                "source_ref": SOURCE_REF,
                "checkpoint": current_cp,
            }
        ),
    )
    assert json.loads(completed.stdout) == expected
    assert _new_receipts(value) == 1


def test_history_corrupt_between_snapshot_and_hydrate_rolls_back_new_receipt(
    history: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _compose_new(history)
    original = value["sources"].assert_current

    def corrupt(binding: Any, snapshot: Any) -> None:
        with sqlite3.connect(value["path"]) as db:
            db.execute("drop trigger hydration_receipt_immutable_update")
            db.execute("update hydration_receipt set receipt_digest=?", (digest("corrupt"),))
        original(binding, snapshot)

    monkeypatch.setattr(value["sources"], "assert_current", corrupt)
    with pytest.raises(PolicyViolation, match="receipt integrity"):
        value["service"].hydrate(_request(idempotency_key="new-startup"))
    assert _new_receipts(value) == 0


def test_same_work_different_run_is_explicit_incompatible_not_stale_content(
    history: dict[str, Any],
) -> None:
    old = history["old"]["binding"]
    with sqlite3.connect(history["path"]) as db:
        config = db.execute(
            "select config_revision_id from run where id=?", (old.run_id,)
        ).fetchone()[0]
    plan = digest("new-current-plan")
    with history["operational"].unit_of_work() as uow:
        run = uow.create_run(
            work_item_id=old.work_item_id,
            config_revision_id=config,
            source_snapshot_id=old.source_snapshot_id,
            plan_digest=plan,
            budget={"max_seconds": 60},
        )
        uow.commit()
    new = replace(
        history["binding"],
        session_id=str(uuid4()),
        external_session_id=str(uuid4()),
        run_id=run.id,
        plan_digest=plan,
    )
    history["base"].bind_session(new)
    selected, report = history["checkpoints"].snapshot(new, observed_at=NOW)
    assert selected is None
    assert report["state"] == "incompatible-history"
    assert "checkpoint_digest" not in report


@pytest.mark.parametrize("field", ["project_id", "realm_id", "work_item_id"])
def test_foreign_history_existence_is_not_disclosed(history: dict[str, Any], field: str) -> None:
    with sqlite3.connect(history["path"]) as db:
        db.execute("drop trigger continuity_session_binding_immutable_update")
        db.execute(
            f"update continuity_session_binding set {field}=? where session_id=?",
            (str(uuid4()), history["old"]["binding"].session_id),
        )
    selected, report = _snapshot(history)
    assert selected is None
    assert report == {"state": "fresh-empty", "grants_authority": False, "fragment_count": 0}


@pytest.mark.parametrize("owner", ["old", "current"])
def test_pending_runtime_does_not_become_checkpoint_authority(
    history: dict[str, Any], owner: str
) -> None:
    binding = history["old"]["binding"] if owner == "old" else history["binding"]
    SQLiteLocalRuntimeStore(history["path"]).enqueue(
        idempotency_key="pending-runtime",
        payload={"operation": "inspect", "session_id": binding.session_id},
    )
    with pytest.raises(PolicyViolation, match="pending"):
        _snapshot(history)


@pytest.mark.parametrize("include_checkpoint", [False, True])
def test_optional_checkpoint_budget_reports_actual_selection_not_only_availability(
    history: dict[str, Any],
    include_checkpoint: bool,
) -> None:
    value = _compose_new(history)
    snap = value["sources"].snapshot(value["binding"], _request())
    required = sum(c.token_count for c in snap.candidates if c.required)
    optional = sum(
        c.token_count for c in snap.candidates if c.kind is ContextCandidateKind.CHECKPOINT
    )
    result = value["service"].hydrate(
        _request(token_budget=required + (optional if include_checkpoint else 0))
    )
    assert result["prior_checkpoint"]["fragment_count"] == 1
    assert result["prior_checkpoint"]["selected_count"] == int(include_checkpoint)
    assert ("prior-checkpoint" in result["remaining_gates"]) is not include_checkpoint
    assert result["token_count"] == required + (optional if include_checkpoint else 0)
    context = json.loads(
        _row(value, "context_manifest", "manifest_digest", result["manifest_digest"])["body_json"]
    )["context"]
    assert sum(c["kind"] == "checkpoint" for c in context["selected_provenance"]) == int(
        include_checkpoint
    )


def test_fresh_scan_is_not_claimed_as_checkpoint_inheritance(startup: dict[str, Any]) -> None:
    checkpoints = SQLiteStartupCheckpointSource(startup["base"])
    source = SQLiteStartupSourceResolver(
        startup["base"], startup["sources"].project_sources, checkpoints=checkpoints
    )
    startup["base"].source_resolver = source
    result = LocalStartupService(startup["lifecycle"], source).hydrate(_request())
    assert result["prior_checkpoint"]["state"] == "fresh-empty"
    assert (
        result["prior_checkpoint"]["fragment_count"]
        == result["prior_checkpoint"]["selected_count"]
        == 0
    )
    assert result["grants_authority"] is False


def test_new_latest_rejects_fresh_same_key_rebind_but_old_persisted_resume_stays_pinned(
    history: dict[str, Any],
) -> None:
    value = _compose_new(history)
    first = value["service"].hydrate(_request(idempotency_key="new-session-once"))
    checkpoint = _checkpoint(value, first["manifest_digest"])
    before = value["base"].resume(value["binding"], checkpoint)
    old = history["old"]["binding"]
    value["base"].checkpoint(
        old,
        expected_tail=value["base"].tail(old),
        context_digest=history["old_manifest"],
        idempotency_key="newer-predecessor-cp",
        spool_digests=value["base"].spool_digests(old),
    )
    with pytest.raises(PolicyViolation, match="replay payload drift"):
        value["service"].hydrate(_request(idempotency_key="new-session-once"))
    assert _new_receipts(value) == 1
    assert value["base"].resume(value["binding"], checkpoint) == before


def test_broken_newest_checkpoint_does_not_silently_fall_back_to_older_good_evidence(
    history: dict[str, Any],
) -> None:
    old = history["old"]["binding"]
    newer = history["base"].checkpoint(
        old,
        expected_tail=history["base"].tail(old),
        context_digest=history["old_manifest"],
        idempotency_key="newest-corrupt",
        spool_digests=history["base"].spool_digests(old),
    )
    with sqlite3.connect(history["path"]) as db:
        db.execute("drop trigger continuity_checkpoint_immutable_update")
        db.execute(
            "update continuity_checkpoint set body_json='{}' where checkpoint_digest=?", (newer,)
        )
    with pytest.raises(PolicyViolation):
        _snapshot(history)


def test_metadata_limits_refs_to_sixteen_without_losing_total_omission_count(
    history: dict[str, Any],
) -> None:
    old = history["old"]
    binding = old["binding"]
    snapshot = old["sources"].snapshot(binding, _request())
    candidates = list(snapshot.candidates)
    fragments = dict(snapshot.fragments)
    template = next(c for c in candidates if c.kind is ContextCandidateKind.SOURCE_SLICE)
    lines = history["text"].splitlines(keepends=True)
    ranges = [(1, last) for last in range(1, len(lines))]
    ranges += [(first, len(lines)) for first in range(2, len(lines))]
    for first, last in ranges:
        ref = f"{SOURCE_REF}#L{first}-L{last}"
        text = old["sources"].project_sources.read_fragment(binding, ref)
        item = replace(
            template,
            candidate_id=f"bounded-prefix-{first}-{last}",
            required=False,
            source_ref=ref,
            content_digest=digest(text),
            token_count=len(text.encode()),
        )
        candidates.append(item)
        fragments[item.candidate_id] = text
    ranking = ContextRankingRequest(
        role="builder",
        target_identity_refs=(f"work/{binding.work_item_id}",),
        step_scope_ref=None,
        work_scope_ref=f"work/{binding.work_item_id}",
        project_scope_ref=f"project/{binding.project_id}",
        realm_scope_ref=f"realm/{binding.realm_id}",
        current_source_revision=history["revision"],
        compatible_source_revisions=tuple(sorted({c.source_revision for c in candidates})),
        task_terms=(),
        tokenizer_profile_digest=DEFAULT_TOKENIZER_PROFILE_DIGEST,
    )
    manifest = compile_context_v2(
        tuple(candidates),
        ranking_request=ranking,
        token_budget=131072,
        minimum_authority=AuthorityLevel.OBSERVED,
        now=NOW,
        contents=fragments,
        ranking_snapshot_digest=digest(ranking.body()),
        candidate_set_digest=digest([c.candidate_digest for c in candidates]),
    )
    selected = {item.candidate_id for item in manifest.selected}
    assert len(selected) > 16
    context = LocalContext(
        manifest,
        tuple((k, v) for k, v in fragments.items() if k in selected),
        ranking,
        tuple(c for c in candidates if c.candidate_id in selected),
    )
    context_digest = old["base"].hydrate(binding, context, idempotency_key="many-real-slices")
    checkpoint = old["base"].checkpoint(
        binding,
        expected_tail=old["base"].tail(binding),
        context_digest=context_digest,
        idempotency_key="many-slice-checkpoint",
        spool_digests=old["base"].spool_digests(binding),
    )
    (candidate, text), report = _snapshot(history)
    body = json.loads(text)
    assert report["checkpoint_digest"] == checkpoint == candidate.source_revision
    assert len(body["source_refs"]) == 16
    assert body["source_ref_count"] == len(selected)
    assert body["omitted_source_ref_count"] == len(selected) - 16
    assert history["text"] not in text and len(text.encode()) <= 16384


def test_tied_creation_timestamps_use_deterministic_checkpoint_digest_order(
    history: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_at = _row(history, "continuity_checkpoint", "checkpoint_digest", history["checkpoint"])[
        "created_at"
    ]
    monkeypatch.setattr("zekam.infrastructure.sqlite.local_continuity._now", lambda: created_at)
    old = history["old"]["binding"]
    options = [history["checkpoint"]]
    for key in ("same-clock-a", "same-clock-b"):
        options.append(
            history["base"].checkpoint(
                old,
                expected_tail=history["base"].tail(old),
                context_digest=history["old_manifest"],
                idempotency_key=key,
                spool_digests=history["base"].spool_digests(old),
            )
        )
    assert _snapshot(history)[1]["checkpoint_digest"] == max(options)


def test_oversized_historical_ledger_is_rejected_before_loading_all_events(
    history: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = history["old"]["binding"]
    with sqlite3.connect(history["path"]) as db:
        db.executemany(
            "insert into session_event values(?,?,?,?,?)",
            (
                (
                    str(uuid4()),
                    old.session_id,
                    "USER_TURN_COMMITTED",
                    digest(f"bound-event-{i}"),
                    NOW.isoformat(),
                )
                for i in range(10000)
            ),
        )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Oversized historical ledger should reject before event materialization")

    monkeypatch.setattr(history["base"], "_events", forbidden)
    with pytest.raises(PolicyViolation, match="bounded verification"):
        _snapshot(history)


@pytest.mark.parametrize("value", [None, [], True, "provenance"])
def test_pinned_provenance_requires_typed_object(history: dict[str, Any], value: Any) -> None:
    with pytest.raises(ValidationFailed):
        history["checkpoints"](history["binding"], value)
