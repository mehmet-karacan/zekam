"""Independent bounded SessionStart acceptance from real read-only Akilli Kasa source."""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.home import HomeLayout
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_service import LocalLifecycleContinuity
from zekam.application.local_continuity_startup import LocalStartupService, StartupRequest
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import ContextCandidateKind
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.codex_lifecycle import parse_codex_hook_input
from zekam.infrastructure.clients.local_continuity_decoder import validate_reviewed_control_entry
from zekam.infrastructure.local_continuity_source import ProjectContinuitySourceResolver
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_startup import SQLiteStartupSourceResolver
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest
from zekam.infrastructure.sqlite.operational_schema import bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

ROOT = Path("/Users/mkaracan/Projeler/akilli-kasa")
SOURCE_REF = "src/akilli_kasa/api/saglik.py"
NOW = dt.datetime(2026, 9, 2, 18, tzinfo=dt.UTC)
POLICY = {
    "runtime": {
        "network_default": "deny",
        "permission_profile": "workspace-write-no-network",
    }
}
PAYLOAD = {
    "summary": f"Inspect the actual Akilli Kasa source {SOURCE_REF}.",
    "acceptance_criteria": ["The health endpoint evidence must cite its real source."],
}


def _request(**overrides: Any) -> StartupRequest:
    values: dict[str, Any] = {
        "source_refs": (SOURCE_REF,),
        "token_budget": 16384,
        "idempotency_key": "startup-actual-health",
        "observed_at": NOW,
    }
    return StartupRequest(**(values | overrides))


def _stage_start(value: dict[str, Any], *, drain: bool = True) -> None:
    parsed = parse_codex_hook_input(
        canonical_json(
            {
                "session_id": value["binding"].external_session_id,
                "hook_event_name": "SessionStart",
                "source": "startup",
                "permission_mode": "default",
            }
        )
    )
    value["spool"].stage(
        parsed.observation_body(),
        delivery_id=parsed.delivery_id(occurrence_id=str(uuid4())),
        occurred_at=NOW,
    )
    if drain:
        value["lifecycle"].drain()


@pytest.fixture
def startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    if not (ROOT / SOURCE_REF).is_file():
        pytest.skip("The real read-only Akilli Kasa bounded source is unavailable")

    def no_network(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("SessionStart must not invoke providers or the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    text = (ROOT / SOURCE_REF).read_text(encoding="utf-8")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    path = tmp_path / "operational.db"
    bootstrap(path)
    operational = SQLiteOperationalStore(path)
    with operational.unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest(POLICY),
            task_digest=digest("wp08-startup"),
            sanitized_config=POLICY,
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
            config_digest=digest("bounded-akilli-health"),
        )
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Akilli Kasa health evidence",
            state="ready",
            payload=PAYLOAD,
            payload_digest=digest(PAYLOAD),
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            source_snapshot_id=snapshot.id,
            plan_digest=digest("wp08-startup-plan"),
            budget={"max_seconds": 60},
        )
        uow.commit()
    realm = str(uuid4())
    with sqlite3.connect(path) as db:
        db.execute(
            "insert into project_knowledge_realm values(?,?,?)",
            (
                project.id,
                realm,
                NOW.isoformat(),
            ),
        )
    binding = ContinuityBinding(
        str(uuid4()),
        str(uuid4()),
        project.id,
        realm,
        "codex",
        "macbook",
        snapshot.id,
        digest("wp08-startup"),
        digest("wp08-startup-plan"),
        digest(POLICY),
        work.id,
        run.id,
    )
    base = SQLiteContinuityStore(path)
    base.bind_session(binding)
    project_sources = ProjectContinuitySourceResolver(
        ROOT,
        project_id=project.id,
        realm_id=realm,
        source_snapshot_id=snapshot.id,
        allowed_paths=(SOURCE_REF,),
    )
    sources = SQLiteStartupSourceResolver(base, project_sources)
    base.source_resolver = sources
    home = tmp_path / "startup-home"
    HomeLayout(home).ensure()
    spool = ClientLifecycleSpool(home, client_id=binding.client_id)
    lifecycle = LocalLifecycleContinuity(
        base,
        spool,
        binding,
        source_probe=lambda: digest((ROOT / SOURCE_REF).read_text()),
        entry_validator=validate_reviewed_control_entry,
    )
    result = {
        "path": path,
        "base": base,
        "binding": binding,
        "sources": sources,
        "lifecycle": lifecycle,
        "service": LocalStartupService(lifecycle, sources),
        "spool": spool,
        "home": home,
        "revision": revision,
        "text": text,
        "operational": operational,
        "source_binding": source.id,
    }
    _stage_start(result)
    return result


def _receipts(value: dict[str, Any]) -> tuple[int, int]:
    with sqlite3.connect(value["path"]) as db:
        return (
            db.execute("select count(*) from context_manifest").fetchone()[0],
            db.execute("select count(*) from hydration_receipt").fetchone()[0],
        )


def test_real_startup_keeps_truthful_source_revisions_and_exact_bound_fragments(
    startup: dict[str, Any],
) -> None:
    before = logical_database_digest(startup["path"])
    snap = startup["sources"].snapshot(startup["binding"], _request())
    assert logical_database_digest(startup["path"]) == before
    by_kind = {item.kind: item for item in snap.candidates}
    assert set(by_kind) == {
        ContextCandidateKind.SOURCE_SLICE,
        ContextCandidateKind.SYSTEM_POLICY,
        ContextCandidateKind.WORK_CONTRACT,
        ContextCandidateKind.RUN_STATUS,
    }
    assert len(snap.candidates) == len(snap.fragments) == 4
    assert all(item.required for item in snap.candidates)
    source = by_kind[ContextCandidateKind.SOURCE_SLICE]
    assert source.source_revision == startup["revision"]
    assert dict(snap.fragments)[source.candidate_id] == startup["text"]
    for kind in set(by_kind) - {ContextCandidateKind.SOURCE_SLICE}:
        assert by_kind[kind].source_revision != startup["revision"]
    for item in snap.candidates:
        resolved = startup["sources"](startup["binding"], item.provenance_body)
        assert resolved == dict(snap.fragments)[item.candidate_id]
        assert digest(resolved) == item.content_digest
    startup["sources"].assert_current(startup["binding"], snap)


def test_startup_persists_one_bounded_manifest_and_exact_replay(startup: dict[str, Any]) -> None:
    before = (ROOT / SOURCE_REF).read_bytes()
    first = startup["service"].hydrate(_request())
    second = startup["service"].hydrate(_request())
    assert first["manifest_digest"] == second["manifest_digest"]
    assert first["scope"] == "required-startup-fragments"
    assert 0 < first["token_count"] <= _request().token_budget
    assert first["remaining_gates"]  # This increment cannot pretend full WP08 completion.
    assert _receipts(startup) == (1, 1)
    assert (ROOT / SOURCE_REF).read_bytes() == before


@pytest.mark.parametrize("value", [None, "4096", True, 0, -1, 131073])
def test_startup_budget_rejects_wrong_types_and_boundaries(value: object) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        _request(token_budget=value)


@pytest.mark.parametrize("refs", [None, [], (), (SOURCE_REF, SOURCE_REF), (None,), ("",)])
def test_startup_source_refs_are_nonempty_unique_bounded_tuple(refs: object) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        _request(source_refs=refs)


@pytest.mark.parametrize("value", [None, "", True, 42, "../escape", "/Users/private/source"])
def test_startup_idempotency_key_rejects_nonportable_values(value: object) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        _request(idempotency_key=value)


@pytest.mark.parametrize("value", [None, "2026-09-02", NOW.replace(tzinfo=None), True])
def test_startup_time_requires_explicit_typed_timezone(value: object) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        _request(observed_at=value)


def test_required_fragment_budget_failure_cannot_commit_partial_receipt(
    startup: dict[str, Any],
) -> None:
    before = logical_database_digest(startup["path"])
    with pytest.raises((PolicyViolation, ValidationFailed)):
        startup["service"].hydrate(_request(token_budget=1))
    assert _receipts(startup) == (0, 0)
    assert logical_database_digest(startup["path"]) == before


@pytest.mark.parametrize("ref", ["README.md", "../escape.py", "src/foreign.py"])
def test_startup_never_reads_outside_exact_real_source_allowlist(
    startup: dict[str, Any],
    ref: str,
) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        startup["service"].hydrate(_request(source_refs=(ref,)))
    assert _receipts(startup) == (0, 0)


@pytest.mark.parametrize(
    "kind",
    [
        ContextCandidateKind.SYSTEM_POLICY,
        ContextCandidateKind.WORK_CONTRACT,
        ContextCandidateKind.RUN_STATUS,
    ],
)
@pytest.mark.parametrize("field", ["source_ref", "revision", "digest", "scope_ref"])
def test_operational_provenance_cannot_be_relabelled_by_caller(
    startup: dict[str, Any],
    kind: ContextCandidateKind,
    field: str,
) -> None:
    snap = startup["sources"].snapshot(startup["binding"], _request())
    candidate = next(item for item in snap.candidates if item.kind is kind)
    body = dict(candidate.provenance_body)
    body[field] = digest("forged") if field == "digest" else "foreign/identity"
    with pytest.raises((PolicyViolation, ValidationFailed)):
        startup["sources"](startup["binding"], body)
    assert _receipts(startup) == (0, 0)


def test_unregistered_source_kind_is_not_a_text_fallback(startup: dict[str, Any]) -> None:
    snap = startup["sources"].snapshot(startup["binding"], _request())
    body = dict(snap.candidates[0].provenance_body)
    body["kind"] = "memory-summary"
    with pytest.raises((PolicyViolation, ValidationFailed)):
        startup["sources"](startup["binding"], body)


@pytest.mark.parametrize("field", ["project_id", "realm_id", "work_item_id", "run_id"])
def test_foreign_operational_binding_cannot_produce_startup(
    startup: dict[str, Any],
    field: str,
) -> None:
    binding = replace(startup["binding"], **{field: str(uuid4())})
    with pytest.raises((PolicyViolation, ValidationFailed)):
        startup["sources"].snapshot(binding, _request())
    assert _receipts(startup) == (0, 0)


@pytest.mark.parametrize("persist_start", [False, True])
def test_missing_required_session_start_is_not_inferred_from_session_row(
    startup: dict[str, Any],
    persist_start: bool,
) -> None:
    binding = replace(
        startup["binding"],
        session_id=str(uuid4()),
        external_session_id=str(uuid4()),
    )
    startup["base"].bind_session(binding)
    lifecycle = LocalLifecycleContinuity(
        startup["base"],
        startup["spool"],
        binding,
        source_probe=lambda: digest((ROOT / SOURCE_REF).read_text()),
        entry_validator=validate_reviewed_control_entry,
    )
    value = startup | {"binding": binding, "lifecycle": lifecycle}
    if persist_start:
        _stage_start(value, drain=False)
    service = LocalStartupService(lifecycle, startup["sources"])
    with pytest.raises((PolicyViolation, ValidationFailed)):
        service.hydrate(_request())
    assert _receipts(startup) == (0, 0)


def test_new_active_config_invalidates_prepared_startup_snapshot(startup: dict[str, Any]) -> None:
    snap = startup["sources"].snapshot(startup["binding"], _request())
    changed = {"runtime": {"network_default": "deny", "permission_profile": "read-only"}}
    with startup["operational"].unit_of_work() as uow:
        uow.activate_config(
            config_digest=digest(changed),
            task_digest=startup["binding"].task_digest,
            sanitized_config=changed,
        )
        uow.commit()
    with pytest.raises((PolicyViolation, ValidationFailed)):
        startup["sources"].assert_current(startup["binding"], snap)
    with pytest.raises((PolicyViolation, ValidationFailed)):
        startup["service"].hydrate(_request())
    assert _receipts(startup) == (0, 0)


def test_corrupt_canonical_work_payload_is_not_hydrated_as_fact(startup: dict[str, Any]) -> None:
    with sqlite3.connect(startup["path"]) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("update work_revision set payload_json='{}'")
        db.execute("drop trigger work_revision_no_update")
        db.execute(
            "update work_revision set payload_json=? where work_item_id=?",
            (
                json.dumps({"summary": "forged", "acceptance_criteria": []}),
                startup["binding"].work_item_id,
            ),
        )
    with pytest.raises((PolicyViolation, ValidationFailed)):
        startup["service"].hydrate(_request())
    assert _receipts(startup) == (0, 0)


def test_source_probe_drift_prevents_startup_receipt(
    startup: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(startup["lifecycle"], "source_probe", lambda: digest("changed"))
    with pytest.raises((PolicyViolation, ValidationFailed)):
        startup["service"].hydrate(_request())
    assert _receipts(startup) == (0, 0)


@pytest.mark.parametrize("scope", ["work_item_id", "run_id", "session_id"])
def test_predecessor_work_runtime_is_not_hidden_by_new_session_identity(
    startup: dict[str, Any],
    scope: str,
) -> None:
    predecessor = replace(
        startup["binding"],
        session_id=str(uuid4()),
        external_session_id=str(uuid4()),
    )
    startup["base"].bind_session(predecessor)
    runtime = SQLiteLocalRuntimeStore(startup["path"])
    runtime.enqueue(
        idempotency_key="previous-inspection",
        payload={
            "operation": "inspect",
            scope: getattr(predecessor, scope),
        },
    )
    before = logical_database_digest(startup["path"])
    with pytest.raises(PolicyViolation, match="predecessor"):
        startup["service"].hydrate(_request())
    assert logical_database_digest(startup["path"]) == before
    assert _receipts(startup) == (0, 0)


def test_unrelated_work_pending_job_does_not_block_bound_startup(startup: dict[str, Any]) -> None:
    runtime = SQLiteLocalRuntimeStore(startup["path"])
    job, _ = runtime.enqueue(
        idempotency_key="unrelated-inspection",
        payload={
            "operation": "inspect",
            "work_item_id": str(uuid4()),
            "run_id": str(uuid4()),
        },
    )
    startup["service"].hydrate(_request())
    assert _receipts(startup) == (1, 1)
    with sqlite3.connect(startup["path"]) as db:
        assert (
            db.execute("select state from local_job where id=?", (job.id,)).fetchone()[0] == "ready"
        )
        assert db.execute("select count(*) from local_lease").fetchone()[0] == 0


@pytest.mark.parametrize("missing", ["delivery", "receipt"])
def test_predecessor_outbox_without_terminal_evidence_is_not_treated_as_settled(
    startup: dict[str, Any],
    missing: str,
) -> None:
    job_id = str(uuid4())
    outbox_id = str(uuid4())
    job_payload = {"operation": "inspect", "work_item_id": startup["binding"].work_item_id}
    payload = {"project_id": startup["binding"].project_id}
    with sqlite3.connect(startup["path"]) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into local_job(id,idempotency_key,payload_json,state,max_attempts,"
            "available_at,terminal_evidence_digest,created_at,updated_at)"
            " values(?,?,?,'completed',1,?,?,?,?)",
            (
                job_id,
                "predecessor-orphan-outbox",
                canonical_json(job_payload),
                NOW.isoformat(),
                digest("terminal-predecessor-job"),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        db.execute(
            "insert into local_outbox values(?,?,?,?,?,?,?)",
            (
                outbox_id,
                job_id,
                "orphan-message",
                "inspection.finished",
                canonical_json(payload),
                digest(payload),
                NOW.isoformat(),
            ),
        )
        if missing == "receipt":
            db.execute(
                "insert into local_outbox_delivery values(?,'delivered',1,"
                "'delivery-claim','worker',1,'past-owner',?,?)",
                (outbox_id, NOW.isoformat(), NOW.isoformat()),
            )
        assert db.execute("select count(*) from local_outbox_receipt").fetchone()[0] == 0
        assert db.execute("select count(*) from local_outbox_delivery").fetchone()[0] == (
            missing == "receipt"
        )
    before = logical_database_digest(startup["path"])
    with pytest.raises(PolicyViolation, match=r"predecessor|outbox|delivery"):
        startup["service"].hydrate(_request())
    assert _receipts(startup) == (0, 0)
    assert logical_database_digest(startup["path"]) == before


def test_config_race_after_last_snapshot_check_is_rejected_inside_hydration(
    startup: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = startup["lifecycle"].hydrate

    def raced(*args: Any, **kwargs: Any) -> Any:
        changed = {"runtime": {"network_default": "deny", "permission_profile": "read-only"}}
        with startup["operational"].unit_of_work() as uow:
            uow.activate_config(
                config_digest=digest(changed),
                task_digest=startup["binding"].task_digest,
                sanitized_config=changed,
            )
            uow.commit()
        return original(*args, **kwargs)

    monkeypatch.setattr(startup["lifecycle"], "hydrate", raced)
    with pytest.raises(PolicyViolation):
        startup["service"].hydrate(_request())
    assert _receipts(startup) == (0, 0)


def test_work_revision_race_after_snapshot_is_not_hydrated_as_current(
    startup: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = startup["lifecycle"].hydrate

    def raced(*args: Any, **kwargs: Any) -> Any:
        with startup["operational"].unit_of_work() as uow:
            uow.transition_work(
                work_item_id=startup["binding"].work_item_id,
                expected_revision=1,
                to_state="active",
                payload_digest=digest(PAYLOAD),
                event_digest=digest("actual-startup-work-progress"),
            )
            uow.commit()
        return original(*args, **kwargs)

    monkeypatch.setattr(startup["lifecycle"], "hydrate", raced)
    with pytest.raises(PolicyViolation):
        startup["service"].hydrate(_request())
    assert _receipts(startup) == (0, 0)


def test_concurrent_exact_startups_publish_only_one_hydration_receipt(
    startup: dict[str, Any],
) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: startup["service"].hydrate(_request()), range(4)))
    assert len({item["manifest_digest"] for item in results}) == 1
    assert _receipts(startup) == (1, 1)


_STARTUP_CHILD = """
import datetime as dt, json, os, signal, socket, sys
from contextlib import contextmanager
from pathlib import Path
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_service import LocalLifecycleContinuity
from zekam.application.local_continuity_startup import LocalStartupService, StartupRequest
from zekam.domain.canonical import canonical_json, digest
from zekam.infrastructure.clients.local_continuity_decoder import validate_reviewed_control_entry
from zekam.infrastructure.local_continuity_source import ProjectContinuitySourceResolver
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_startup import SQLiteStartupSourceResolver
def forbidden(*args, **kwargs):
    raise AssertionError('Startup network forbidden')
socket.socket.connect = forbidden
socket.create_connection = forbidden
path, home, raw_binding, timing = sys.argv[1:]
binding = ContinuityBinding(**json.loads(raw_binding))
root = Path('/Users/mkaracan/Projeler/akilli-kasa')
ref = 'src/akilli_kasa/api/saglik.py'
base = SQLiteContinuityStore(Path(path))
project = ProjectContinuitySourceResolver(root, project_id=binding.project_id,
    realm_id=binding.realm_id, source_snapshot_id=binding.source_snapshot_id, allowed_paths=(ref,))
sources = SQLiteStartupSourceResolver(base, project)
base.source_resolver = sources
spool = ClientLifecycleSpool(Path(home), client_id=binding.client_id)
lifecycle = LocalLifecycleContinuity(base, spool, binding,
    source_probe=lambda: digest((root/ref).read_text()),
    entry_validator=validate_reviewed_control_entry)
if timing != 'none':
    original = base._transaction
    @contextmanager
    def interrupted():
        kill_after = False
        with original() as db:
            yield db
            if db.execute('select count(*) from hydration_receipt').fetchone()[0]:
                if timing == 'before-commit':
                    os.kill(os.getpid(), signal.SIGKILL)
                kill_after = True
        if kill_after:
            os.kill(os.getpid(), signal.SIGKILL)
    base._transaction = interrupted
request = StartupRequest((ref,),16384,'startup-actual-health',
    dt.datetime(2026,9,2,18,tzinfo=dt.UTC))
print(canonical_json(LocalStartupService(lifecycle,sources).hydrate(request)))
"""


def _startup_child(
    startup: dict[str, Any], timing: str = "none"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _STARTUP_CHILD,
            str(startup["path"]),
            str(startup["home"]),
            canonical_json(asdict(startup["binding"])),
            timing,
        ],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_fresh_process_reuses_exact_durable_startup_manifest(startup: dict[str, Any]) -> None:
    initial = startup["service"].hydrate(_request())
    before = logical_database_digest(startup["path"])
    child = _startup_child(startup)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout)["manifest_digest"] == initial["manifest_digest"]
    assert _receipts(startup) == (1, 1)
    assert logical_database_digest(startup["path"]) == before


@pytest.mark.parametrize("timing,expected", [("before-commit", (0, 0)), ("after-commit", (1, 1))])
def test_process_death_at_hydration_commit_never_leaves_partial_evidence(
    startup: dict[str, Any],
    timing: str,
    expected: tuple[int, int],
) -> None:
    child = _startup_child(startup, timing)
    assert child.returncode == -9, child.stderr
    assert _receipts(startup) == expected
    restarted = _startup_child(startup)
    assert restarted.returncode == 0, restarted.stderr
    assert _receipts(startup) == (1, 1)
    assert (
        startup["service"].hydrate(_request())["manifest_digest"]
        == json.loads(restarted.stdout)["manifest_digest"]
    )
