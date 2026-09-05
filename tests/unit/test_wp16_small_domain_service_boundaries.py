from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest

import zekam.infrastructure.doctor.sqlite_checks as sqlite_checks
from zekam.application.agent_control import AgentControl
from zekam.application.local_embedding_composition import (
    _dissimilarity,
    project_embedding_probe_fixture,
)
from zekam.application.model_contract_service import ModelContractRunner
from zekam.domain.agent_graph import ChildRuntimeStatus
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.knowledge import Locator, UnitKind
from zekam.domain.memory_telemetry import (
    MemoryEffectiveness,
    MemoryUsageEvent,
    MemoryUsageOutcome,
    memory_source_ref,
)
from zekam.domain.model_contract import ContractObservation
from zekam.domain.model_inventory import Modality
from zekam.domain.process_observation import (
    ObservedClient,
    ProcessIdentity,
    ProcessObservation,
    ProcessObservationSnapshot,
)
from zekam.domain.retrieval import Chunk
from zekam.domain.security import DataClassification
from zekam.infrastructure.doctor.sqlite_checks import CapabilityCheck, PersistenceCheck
from zekam.infrastructure.doctor.storage_checks import ObjectStoreCheck
from zekam.infrastructure.sqlite.operational_schema import SCHEMA_VERSION

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)
IDS = tuple(UUID(f"018f0000-0000-7000-8000-{index:012d}") for index in range(1, 24))
DIGEST = digest("bound")


def test_agent_control_scope_trace_and_terminal_transition_paths() -> None:
    root = SimpleNamespace(id=IDS[0], realm_id=IDS[1])
    store = Mock()
    store.create_root.return_value = (IDS[0], True)
    store.reserve_spawn.return_value = (IDS[2], True)
    store.transition_child.return_value = True
    store.send_message.return_value = (IDS[3], True)
    store.snapshot.return_value = {"children": 1}
    trace = Mock()
    control = AgentControl(cast(Any, root), cast(Any, store), cast(Any, trace))
    assert control.initialize() == (IDS[0], True)

    edge = SimpleNamespace(
        id=IDS[2],
        root_id=IDS[0],
        realm_id=IDS[1],
        parent_assignment_id=IDS[4],
        child_assignment_id=IDS[5],
        created_at=NOW,
        body=lambda: {"edge": "safe"},
    )
    assert control.reserve(cast(Any, edge)) == (IDS[2], True)
    edge.root_id = IDS[9]
    with pytest.raises(ValueError, match="root scope"):
        control.reserve(cast(Any, edge))
    edge.root_id = IDS[0]
    edge.realm_id = IDS[9]
    with pytest.raises(ValueError, match="root scope"):
        control.reserve(cast(Any, edge))

    for status in (
        ChildRuntimeStatus.ACTIVE,
        ChildRuntimeStatus.COMPLETED,
        ChildRuntimeStatus.FAILED,
        ChildRuntimeStatus.CANCELLED,
    ):
        assert control.transition(IDS[2], status=status, occurred_at=NOW, input_tokens_used=1)

    message = SimpleNamespace(
        id=IDS[3],
        root_id=IDS[0],
        realm_id=IDS[1],
        sender_assignment_id=IDS[4],
        recipient_assignment_id=IDS[5],
        message_digest=DIGEST,
        payload_schema="schema/v1",
        context_digest=DIGEST,
        created_at=NOW,
    )
    assert control.send(cast(Any, message)) == (IDS[3], True)
    message.realm_id = IDS[9]
    with pytest.raises(ValueError, match="root scope"):
        control.send(cast(Any, message))
    assert control.status() == {"children": 1}
    assert trace.record.call_count == 6

    without_trace = AgentControl(cast(Any, root), cast(Any, store))
    edge.realm_id = IDS[1]
    assert without_trace.reserve(cast(Any, edge)) == (IDS[2], True)
    assert without_trace.transition(IDS[2], status=ChildRuntimeStatus.ACTIVE, occurred_at=NOW)
    message.realm_id = IDS[1]
    assert without_trace.send(cast(Any, message)) == (IDS[3], True)


def _chunk(identifier: str, text: str, line: int | None = None) -> Chunk:
    return Chunk(
        identifier,
        "document",
        text,
        Locator(relative_path="src/service.py", line_start=line, line_end=line),
        UnitKind.CODE,
        max(1, len(text.split())),
        0,
    )


def test_embedding_probe_selection_empty_duplicate_and_locator_branches() -> None:
    with pytest.raises(ValidationFailed, match="en az iki"):
        project_embedding_probe_fixture((), classification=DataClassification.LOCAL_ONLY)
    with pytest.raises(ValidationFailed, match="en az iki"):
        project_embedding_probe_fixture(
            cast(Any, [_chunk("a", "one"), _chunk("b", "two")]),
            classification=DataClassification.LOCAL_ONLY,
        )
    with pytest.raises(ValidationFailed, match="bounded iki"):
        project_embedding_probe_fixture(
            (_chunk("a", "one"), _chunk("b", "x" * 12_001)),
            classification=DataClassification.LOCAL_ONLY,
        )
    with pytest.raises(ValidationFailed, match="duplicate"):
        project_embedding_probe_fixture(
            (_chunk("a", "same"), _chunk("b", "same")),
            classification=DataClassification.LOCAL_ONLY,
        )
    fixture = project_embedding_probe_fixture(
        (_chunk("a", "alpha beta", 7), _chunk("b", "gamma delta")),
        classification=DataClassification.LOCAL_ONLY,
    )
    assert fixture.source_refs == ("src/service.py:7#a", "src/service.py#b")
    assert _dissimilarity("", "") == 0.0
    assert _dissimilarity("alpha", "beta") == 1.0


def test_model_contract_runner_rejects_modality_and_missing_evidence_then_records() -> None:
    record = SimpleNamespace(modality=Modality.AUDIO_TRANSCRIPTION)
    health = SimpleNamespace(
        inventory=SimpleNamespace(get=Mock(return_value=record)), record_capability=Mock()
    )
    adapter = SimpleNamespace(observe=Mock())
    runner = ModelContractRunner(cast(Any, health), cast(Any, adapter))

    adapter.observe.return_value = ContractObservation(modality=Modality.CHAT)
    with pytest.raises(ValidationFailed, match="modalitesiyle"):
        runner.run("model")
    adapter.observe.return_value = ContractObservation(modality=Modality.AUDIO_TRANSCRIPTION)
    with pytest.raises(ValidationFailed, match="digest ister"):
        runner.run("model")
    adapter.observe.return_value = ContractObservation(
        modality=Modality.AUDIO_TRANSCRIPTION,
        transcript_pairs=(("same", "same"),),
        fixture_digest=digest("fixture"),
        response_digest=digest("response"),
    )
    assert runner.run("model").verified
    health.record_capability.assert_called_once()


def test_process_observation_validation_roots_and_stable_digest() -> None:
    with pytest.raises(ValidationFailed, match="pozitif"):
        ProcessIdentity(0, 1)
    with pytest.raises(ValidationFailed, match="pozitif"):
        ProcessIdentity(1, 0)
    identity = ProcessIdentity(42, 1_000_000)
    base = ProcessObservation(identity, None, ObservedClient.CODEX, "codex", "running", NOW)
    assert base.safe_body()["process_id"] == identity.key
    for changes, message in (
        ({"executable": ""}, "basename"),
        ({"executable": "bin/codex"}, "basename"),
        ({"executable": "bin\\codex"}, "basename"),
        ({"cpu_percent": -0.1}, "CPU"),
        ({"rss_bytes": -1}, "RSS"),
        ({"child_process_count": -1}, "Child"),
    ):
        with pytest.raises(ValidationFailed, match=message):
            replace(base, **cast(Any, changes))
    child = replace(base, identity=ProcessIdentity(43, 1_000_001), root=False)
    snapshot = ProcessObservationSnapshot(NOW, (base, child), True, "scan")
    assert snapshot.roots == (base,)
    assert (
        snapshot.source_digest
        == ProcessObservationSnapshot(NOW, (base, child), True, "scan").source_digest
    )


def _usage_event() -> MemoryUsageEvent:
    return MemoryUsageEvent(
        id=IDS[0],
        record_id=IDS[1],
        request_manifest_id=IDS[2],
        invocation_attempt_id=IDS[3],
        invocation_result_id=IDS[4],
        task_plan_id=IDS[5],
        run_id=IDS[6],
        job_id=IDS[7],
        runtime_attempt_id=IDS[8],
        assignment_id=IDS[9],
        step_id="step",
        project_id=IDS[10],
        work_item_id=IDS[11],
        record_digest=DIGEST,
        fragment_digest=DIGEST,
        model_visible_payload_digest=DIGEST,
        context_manifest_digest=DIGEST,
        used_at=NOW,
        event_digest=DIGEST,
    )


def test_memory_telemetry_digest_time_status_and_counter_boundaries() -> None:
    event = _usage_event()
    assert memory_source_ref(event.record_id).endswith(str(event.record_id))
    with pytest.raises(ValidationFailed, match="step"):
        replace(event, step_id="")
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        replace(event, used_at=NOW.replace(tzinfo=None))

    outcome = MemoryUsageOutcome(
        IDS[12],
        event.id,
        IDS[13],
        "step",
        IDS[14],
        IDS[15],
        DIGEST,
        DIGEST,
        DIGEST,
        "verified-success",
        NOW,
        DIGEST,
    )
    with pytest.raises(ValidationFailed, match="status"):
        replace(outcome, outcome_status="failed")
    with pytest.raises(ValidationFailed, match="step"):
        replace(outcome, step_id="")
    with pytest.raises(ValidationFailed, match="timezone-aware"):
        replace(outcome, correlated_at=NOW.replace(tzinfo=None))

    effectiveness = MemoryEffectiveness(event.record_id, DIGEST, 1, 1, 1, NOW, NOW)
    assert effectiveness.verified_success_count == 1
    with pytest.raises(ValidationFailed, match="negatif"):
        replace(effectiveness, usage_count=-1)
    with pytest.raises(ValidationFailed, match="asamaz"):
        replace(effectiveness, verified_success_count=2)


def test_sqlite_doctor_status_matrix_and_capabilities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def state(**changes: object) -> SimpleNamespace:
        values: dict[str, object] = {
            "exists": True,
            "schema_version": SCHEMA_VERSION,
            "integrity_ok": True,
            "schema_ok": True,
        }
        values.update(changes)
        return SimpleNamespace(**values)

    check = PersistenceCheck(tmp_path / "operational.sqlite3", "state/operational.sqlite3")
    monkeypatch.setattr(sqlite_checks, "status", lambda _path: state(exists=False))
    assert check.run().findings[0].code == "sqlite.database-missing"
    for changes in (
        {"integrity_ok": False},
        {"schema_ok": False},
        {"schema_version": -1},
    ):
        monkeypatch.setattr(sqlite_checks, "status", lambda _path, c=changes: state(**c))
        assert check.run().findings[0].code == "sqlite.integrity-or-schema-drift"
    monkeypatch.setattr(sqlite_checks, "status", lambda _path: state())
    assert check.run().status.value == "passed"
    assert CapabilityCheck().run().evidence["fallback"] is False

    missing = ObjectStoreCheck(tmp_path / "missing").run()
    assert missing.status.value == "degraded"
    empty = tmp_path / "objects"
    empty.mkdir()
    assert ObjectStoreCheck(empty).run().status.value == "passed"
