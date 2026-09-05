"""Independent existing-state composition; actual source read-only, no native-hook claim."""

from __future__ import annotations

import json
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
from tests.unit.test_local_continuity_environment import Fixture
from tests.unit.test_local_continuity_environment import environment as environment
from tests.unit.test_local_continuity_startup import (
    NOW,
    ROOT,
    SOURCE_REF,
    _receipts,
    _request,
    _stage_start,
)
from tests.unit.test_local_startup_retrieval_integration import _build, _checkpoint, _records

from zekam.application.home import HomeLayout
from zekam.application.local_continuity_source_plan import ContinuitySourceRecipe
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.claude_lifecycle import (
    CLAUDE_REVIEWED_VERSION,
    parse_claude_hook_input,
)
from zekam.infrastructure.clients.codex_lifecycle import parse_codex_hook_input
from zekam.infrastructure.local_continuity_source_plan import BoundedContinuitySource
from zekam.infrastructure.local_startup_composition import compose_local_startup
from zekam.infrastructure.sqlite.knowledge_index import SQLiteKnowledgeIndex
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_backup import logical_database_digest
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore


@pytest.fixture
def composition(environment: Fixture, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    gate, old = environment

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Composition must not invoke network or providers")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    operational = SQLiteOperationalStore(gate.operational_path)
    with operational.unit_of_work() as uow:
        git = uow.bind_source(
            project_id=old.project_id,
            portable_ref="project/akilli-kasa-git",
            source_kind="git",
        )
        uow.commit()
    recipe = ContinuitySourceRecipe(
        old.project_id,
        old.realm_id,
        git.id,
        (SOURCE_REF,),
        old.task_digest,
        old.policy_digest,
    )
    source = BoundedContinuitySource(ROOT, recipe)
    plan = source.capture()
    snapshot = source.apply(operational, plan, expected_plan_digest=plan.content_digest)
    with sqlite3.connect(gate.operational_path) as db:
        config_id = db.execute("select id from config_revision where active=1").fetchone()[0]
    payload = {
        "summary": f"Inspect real Akilli Kasa health source {SOURCE_REF}",
        "acceptance_criteria": ["Cite the exact bounded health source."],
    }
    with operational.unit_of_work() as uow:
        work = uow.create_work(
            project_id=old.project_id,
            kind="task",
            title="Bounded startup composition",
            state="ready",
            payload=payload,
            payload_digest=digest(payload),
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config_id,
            source_snapshot_id=snapshot.id,
            plan_digest=digest("independent-composition-plan"),
            budget={"max_seconds": 60},
        )
        uow.commit()
    binding = replace(
        old,
        session_id=str(uuid4()),
        external_session_id=str(uuid4()),
        source_snapshot_id=snapshot.id,
        plan_digest=run.plan_digest,
        work_item_id=work.id,
        run_id=run.id,
    )
    SQLiteContinuityStore(gate.operational_path).bind_session(binding)
    composed = compose_local_startup(gate, binding, source)
    return {
        "gate": gate,
        "path": gate.operational_path,
        "home": gate.home,
        "binding": binding,
        "source": source,
        "plan": plan,
        "recipe": recipe,
        "operational": operational,
        "composed": composed,
        "base": composed.lifecycle.store,
        "sources": composed.sources,
        "lifecycle": composed.lifecycle,
        "spool": composed.lifecycle.spool,
        "text": (ROOT / SOURCE_REF).read_text(),
        "revision": plan.revision_ref,
    }


@pytest.fixture
def ready(composition: dict[str, Any]) -> dict[str, Any]:
    _stage_start(composition, drain=False)
    assert composition["composed"].drain() == 1
    return composition


def _recompose(value: dict[str, Any], **changes: Any) -> Any:
    args = {"environment": value["gate"], "binding": value["binding"], "source": value["source"]}
    return compose_local_startup(**(args | changes))


def test_composition_construction_is_existing_state_only(
    composition: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = logical_database_digest(composition["path"])

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Composition attempted implicit bootstrap, bind, source apply or layout write")

    monkeypatch.setattr(HomeLayout, "ensure", forbidden)
    monkeypatch.setattr(BoundedContinuitySource, "apply", forbidden)
    monkeypatch.setattr(SQLiteContinuityStore, "bind_session", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    _recompose(composition)
    assert logical_database_digest(composition["path"]) == before
    assert _receipts(composition) == (0, 0)


def test_real_hydration_labels_are_structural_only_and_replay_exact(ready: dict[str, Any]) -> None:
    before = (ROOT / SOURCE_REF).read_bytes()
    first = ready["composed"].hydrate(_request())
    second = ready["composed"].hydrate(_request())
    assert first == second
    assert first["client_evidence"] == "reviewed-structural-observations-only"
    assert first["installed_client_lifecycle_proven"] is False
    assert first["hook_activation"] == "not-performed"
    assert "installed-client-lifecycle" in first["remaining_gates"]
    assert first["provider_called"] is first["grants_authority"] is False
    assert first["selected_count"] == 4 and _receipts(ready) == (1, 1)
    assert (ROOT / SOURCE_REF).read_bytes() == before


def test_combined_environment_and_source_evidence_digest_is_self_consistent(
    composition: dict[str, Any],
) -> None:
    report = composition["sources"].preflight(composition["binding"])
    assert report["source_capture_digest"] == composition["plan"].content_digest
    assert report["atomic_filesystem_snapshot"] is False
    body = dict(report)
    fingerprint = body.pop("evidence_digest")
    assert digest(body) == fingerprint


def test_requested_missing_index_is_explicitly_unavailable(ready: dict[str, Any]) -> None:
    result = ready["composed"].hydrate(_request(retrieval_query="SaglikYaniti"))
    assert result["retrieval"]["state"] == "abstained-index-unavailable"
    assert result["retrieval"]["reason"] == "index-not-configured"
    assert result["retrieval"]["searched_channels"] == []
    assert result["retrieval"]["source_bytes_verified"] is False
    assert result["retrieval"]["dense"] == "not-invoked"
    assert "knowledge-retrieval" in result["remaining_gates"]


@pytest.mark.parametrize("field", ["environment", "binding", "source"])
@pytest.mark.parametrize("value", [None, "", False])
def test_composition_rejects_wrong_typed_inputs(
    composition: dict[str, Any], field: str, value: Any
) -> None:
    before = logical_database_digest(composition["path"])
    with pytest.raises(ValidationFailed):
        _recompose(composition, **{field: value})
    assert logical_database_digest(composition["path"]) == before


@pytest.mark.parametrize("value", [None, "", {}, False])
def test_hydrate_wrong_type_never_creates_receipt(ready: dict[str, Any], value: Any) -> None:
    with pytest.raises(ValidationFailed):
        ready["composed"].hydrate(value)
    assert _receipts(ready) == (0, 0)


@pytest.mark.parametrize("field", ["session_id", "project_id", "realm_id", "source_snapshot_id"])
def test_binding_forgery_cannot_compose_existing_authority(
    composition: dict[str, Any], field: str
) -> None:
    before = logical_database_digest(composition["path"])
    forged = replace(composition["binding"], **{field: str(uuid4())})
    with pytest.raises((ConfigurationError, PolicyViolation, ValidationFailed)):
        _recompose(composition, binding=forged)
    assert logical_database_digest(composition["path"]) == before


def test_opencode_is_explicitly_unsupported_not_downgraded(composition: dict[str, Any]) -> None:
    with pytest.raises(PolicyViolation, match="no reviewed structural decoder"):
        _recompose(composition, binding=replace(composition["binding"], client_id="opencode"))


def test_claude_real_parser_is_supported_but_never_claims_native_proof(
    composition: dict[str, Any],
) -> None:
    binding = replace(
        composition["binding"],
        session_id=str(uuid4()),
        external_session_id=str(uuid4()),
        client_id="claude-code",
    )
    composition["base"].bind_session(binding)
    composed = _recompose(composition, binding=binding)
    parsed = parse_claude_hook_input(
        canonical_json(
            {
                "session_id": binding.external_session_id,
                "hook_event_name": "SessionStart",
                "source": "startup",
            }
        )
    )
    composed.lifecycle.spool.stage(
        parsed.observation_body(),
        delivery_id=parsed.delivery_id(
            occurrence_id=str(uuid4()), client_version=CLAUDE_REVIEWED_VERSION
        ),
        occurred_at=NOW,
    )
    assert composed.drain() == 1
    result = composed.hydrate(_request())
    assert result["installed_client_lifecycle_proven"] is False
    assert result["hook_activation"] == "not-performed"
    assert "installed-client-lifecycle" in result["remaining_gates"]
    assert _receipts(composition) == (1, 1)


def test_canonical_but_forged_structural_observation_cannot_drain(
    composition: dict[str, Any],
) -> None:
    parsed = parse_codex_hook_input(
        canonical_json(
            {
                "session_id": composition["binding"].external_session_id,
                "hook_event_name": "SessionStart",
                "source": "startup",
                "permission_mode": "default",
            }
        )
    )
    forged = parsed.observation_body() | {"wire_digest": digest("forged-wire")}
    composition["spool"].stage(
        forged,
        delivery_id=parsed.delivery_id(occurrence_id=str(uuid4())),
        occurred_at=NOW,
    )
    before = logical_database_digest(composition["path"])
    with pytest.raises(PolicyViolation, match="wire digest drift"):
        composition["composed"].drain()
    assert logical_database_digest(composition["path"]) == before
    assert _receipts(composition) == (0, 0)


def test_wrong_external_session_start_is_not_used_as_bound_start(
    composition: dict[str, Any],
) -> None:
    foreign = composition | {
        "binding": replace(composition["binding"], external_session_id=str(uuid4()))
    }
    _stage_start(foreign, drain=False)
    assert composition["composed"].drain() == 0
    with pytest.raises(PolicyViolation, match="SESSION_START"):
        composition["composed"].hydrate(_request())
    assert _receipts(composition) == (0, 0)


@pytest.mark.parametrize("missing", ["config.yaml", "state/operational.db", "layout.json"])
def test_missing_runtime_inputs_are_not_bootstrapped(
    composition: dict[str, Any], missing: str
) -> None:
    target = composition["home"] / missing
    target.rename(target.with_name(target.name + ".saved"))
    with pytest.raises(ConfigurationError):
        _recompose(composition)
    assert not target.exists()


def test_missing_and_unpersisted_hook_cannot_create_receipt(composition: dict[str, Any]) -> None:
    with pytest.raises(PolicyViolation, match="SESSION_START"):
        composition["composed"].hydrate(_request())
    _stage_start(composition, drain=False)
    with pytest.raises(PolicyViolation, match="unpersisted"):
        composition["composed"].hydrate(_request())
    assert _receipts(composition) == (0, 0)
    assert composition["composed"].drain() == 1
    before_replay = logical_database_digest(composition["path"])
    # Existing bridge contract reports all observed entries, including exact replay.
    assert composition["composed"].drain() == 1
    assert logical_database_digest(composition["path"]) == before_replay
    composition["composed"].hydrate(_request())
    assert _receipts(composition) == (1, 1)


@pytest.mark.parametrize("scope", ["session_id", "run_id", "work_item_id"])
def test_pending_runtime_work_blocks_hydration_without_claiming_it(
    ready: dict[str, Any], scope: str
) -> None:
    SQLiteLocalRuntimeStore(ready["path"]).enqueue(
        idempotency_key="pending-composition-inspection",
        payload={"operation": "inspect", scope: getattr(ready["binding"], scope)},
    )
    before = logical_database_digest(ready["path"])
    with pytest.raises(PolicyViolation):
        ready["composed"].hydrate(_request())
    assert _receipts(ready) == (0, 0)
    assert logical_database_digest(ready["path"]) == before


@pytest.mark.parametrize("entry", ["compose", "drain", "hydrate"])
def test_changed_actual_settings_rejected_at_each_entry(ready: dict[str, Any], entry: str) -> None:
    config = ready["home"] / "config.yaml"
    config.write_text(config.read_text() + "\nruntime:\n  log_level: DEBUG\n")
    before = logical_database_digest(ready["path"])
    with pytest.raises(ConfigurationError, match="settings digest drift"):
        if entry == "compose":
            _recompose(ready)
        elif entry == "drain":
            ready["composed"].drain()
        else:
            ready["composed"].hydrate(_request())
    assert logical_database_digest(ready["path"]) == before


@pytest.mark.parametrize("suffix", ["#L1-L2", "#L0-L2", "#L2-L1", "#L1-L9999", "#", "#bad"])
def test_bounded_line_locators_use_exact_verified_whole_buffer(
    composition: dict[str, Any], suffix: str
) -> None:
    reader = composition["sources"].project_sources
    if suffix == "#L1-L2":
        assert reader.read_fragment(composition["binding"], SOURCE_REF + suffix) == "".join(
            composition["text"].splitlines(keepends=True)[:2]
        )
    else:
        with pytest.raises(ValidationFailed):
            reader.read_fragment(composition["binding"], SOURCE_REF + suffix)


def test_same_size_source_corruption_rejected_before_line_extraction(
    composition: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = composition["text"].encode()
    corrupt = bytes([payload[0] ^ 1]) + payload[1:]
    monkeypatch.setattr(composition["source"], "_read", lambda *_args, **_kwargs: corrupt)
    with pytest.raises(PolicyViolation, match="full source identity drift"):
        composition["sources"].project_sources.read_fragment(
            composition["binding"], SOURCE_REF + "#L1-L2"
        )
    assert _receipts(composition) == (0, 0)


def test_full_source_read_and_line_slice_use_one_capture(
    composition: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def read(*_args: Any, **_kwargs: Any) -> bytes:
        nonlocal calls
        calls += 1
        assert calls == 1, "Source slice reopened after hash verification"
        payload = composition["text"].encode()
        assert isinstance(payload, bytes)
        return payload

    monkeypatch.setattr(composition["source"], "_read", read)
    assert composition["sources"].project_sources.read_fragment(
        composition["binding"], SOURCE_REF + "#L1-L2"
    ) == "".join(composition["text"].splitlines(keepends=True)[:2])
    assert calls == 1


def test_source_revalidated_inside_hydration_writer_window(
    ready: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ready["source"].probe
    locked_calls = 0

    def probe(*args: Any, **kwargs: Any) -> Any:
        nonlocal locked_calls
        try:
            with sqlite3.connect(ready["path"], timeout=0) as db:
                db.execute("begin immediate")
                db.rollback()
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc)
            locked_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(ready["source"], "probe", probe)
    ready["composed"].hydrate(_request())
    assert locked_calls >= 4  # Every selected required fragment was checked under writer ownership.


def test_late_source_failure_rolls_back_all_hydration_rows(
    ready: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ready["source"].probe

    def probe(*args: Any, **kwargs: Any) -> Any:
        try:
            with sqlite3.connect(ready["path"], timeout=0) as db:
                db.execute("begin immediate")
                db.rollback()
        except sqlite3.OperationalError:
            raise PolicyViolation("Injected late source drift under hydration writer") from None
        return original(*args, **kwargs)

    monkeypatch.setattr(ready["source"], "probe", probe)
    with pytest.raises(PolicyViolation, match="late source drift"):
        ready["composed"].hydrate(_request())
    assert _receipts(ready) == (0, 0)


def test_concurrent_same_request_has_one_manifest_and_receipt(ready: dict[str, Any]) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ready["composed"].hydrate(_request()), range(2)))
    assert results[0] == results[1]
    assert _receipts(ready) == (1, 1)


def test_process_restart_recomposes_and_resumes_exact_checkpoint(ready: dict[str, Any]) -> None:
    result = ready["composed"].hydrate(_request())
    checkpoint = _checkpoint(ready, result["manifest_digest"])
    script = """
import json, socket, sys
from pathlib import Path
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_source_plan import ContinuitySourceRecipe
from zekam.infrastructure.local_continuity_environment import LocalContinuityEnvironment
from zekam.infrastructure.local_continuity_source_plan import BoundedContinuitySource
from zekam.infrastructure.local_startup_composition import compose_local_startup
def forbidden(*args, **kwargs): raise AssertionError('No network/provider')
socket.socket.connect=forbidden
socket.create_connection=forbidden
data=json.load(sys.stdin)
gate=LocalContinuityEnvironment(**{k:Path(v) for k,v in data['environment'].items()})
data['recipe']['allowed_paths']=tuple(data['recipe']['allowed_paths'])
source=BoundedContinuitySource(Path(data['root']), ContinuitySourceRecipe(**data['recipe']))
binding=ContinuityBinding(**data['binding'])
composed=compose_local_startup(gate,binding,source)
resumed=composed.lifecycle.store.resume(binding,data['checkpoint'])
print(json.dumps(resumed))
"""
    restarted = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(
            {
                "environment": {k: str(v) for k, v in asdict(ready["gate"]).items()},
                "recipe": asdict(ready["recipe"]),
                "binding": asdict(ready["binding"]),
                "root": str(ROOT),
                "checkpoint": checkpoint,
            }
        ),
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    assert json.loads(restarted.stdout) == ready["base"].resume(ready["binding"], checkpoint)
    assert _receipts(ready) == (1, 1)


@pytest.mark.parametrize("value", [False, {}, "index"])
def test_optional_index_wrong_type_never_silently_downgrades(
    composition: dict[str, Any], value: Any
) -> None:
    with pytest.raises(PolicyViolation, match="exact read-only SQLite"):
        _recompose(composition, index=value)


def test_only_read_only_index_can_supply_source_verified_citation(
    ready: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "structural-index.sqlite3"
    with SQLiteKnowledgeIndex(path, create=True) as writable:
        _build(ready | {"index": writable}, _records(ready), tree_digest=ready["plan"].tree_digest)
        with pytest.raises(PolicyViolation, match="exact read-only SQLite"):
            _recompose(ready, index=writable)
    original_bytes = path.read_bytes()

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Dense/provider path invoked in provider-free startup")

    monkeypatch.setattr(SQLiteKnowledgeIndex, "dense", forbidden)
    with SQLiteKnowledgeIndex(path, read_only=True) as index:
        composed = _recompose(ready, index=index)
        result = composed.hydrate(_request(retrieval_query="SaglikYaniti"))
        assert result["retrieval"]["state"] == "source-verified-candidates"
        assert result["retrieval"]["dense"] == "not-invoked"
        assert result["retrieval"]["source_bytes_verified"] is True
        assert result["retrieval"]["fragment_count"] == 1
        assert result["selected_count"] == 5
    assert path.read_bytes() == original_bytes
    assert _receipts(ready) == (1, 1)


def test_existing_but_empty_read_only_index_is_not_healthy(
    ready: dict[str, Any], tmp_path: Path
) -> None:
    path = tmp_path / "empty-index.sqlite3"
    with SQLiteKnowledgeIndex(path, create=True):
        pass
    before = path.read_bytes()
    with SQLiteKnowledgeIndex(path, read_only=True) as index:
        result = _recompose(ready, index=index).hydrate(_request(retrieval_query="SaglikYaniti"))
    assert result["retrieval"]["state"] == "abstained-index-unavailable"
    assert result["retrieval"]["fragment_count"] == 0
    assert "knowledge-retrieval" in result["remaining_gates"]
    assert path.read_bytes() == before
