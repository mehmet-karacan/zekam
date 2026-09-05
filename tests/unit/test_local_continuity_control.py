"""Independent v3 control integration: real parsers/source and disposable disk state."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity import NOW
from tests.unit.test_local_continuity import continuity as continuity
from tests.unit.test_local_continuity_bridge_close import _hydrate, _parsed, _stage, _summary
from tests.unit.test_local_continuity_bridge_close import bridge as bridge
from tests.unit.test_local_continuity_close import OWNER, _drain_runtime

from zekam.application.local_continuity import ContinuityEvent
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.local_continuity_control import SQLiteContinuityControlStore


@pytest.fixture
def control(bridge: dict[str, Any]) -> dict[str, Any]:
    adapter = SQLiteContinuityControlStore(bridge["base"], bridge["spool"])
    bridge["controls"] = adapter
    bridge["lifecycle"].controls = adapter
    return bridge


def _rows(control: dict[str, Any], table: str) -> list[dict[str, Any]]:
    assert table in {
        "session_event",
        "session_event_detail",
        "continuity_checkpoint",
        "continuity_close_request",
        "continuity_control_event",
        "close_receipt",
    }
    with sqlite3.connect(control["base"].path) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in db.execute(
                f"select * from {table} where session_id=? order by 1",
                (control["binding"].session_id,),
            )
        ]


def _frozen_rows(control: dict[str, Any]) -> str:
    return digest(
        {
            table: _rows(control, table)
            for table in (
                "session_event",
                "session_event_detail",
                "continuity_checkpoint",
                "continuity_close_request",
            )
        }
    )


def _spool_bytes(control: dict[str, Any]) -> dict[str, bytes]:
    root = control["spool"].root
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def _freeze(control: dict[str, Any], *, internal_event: bool = False) -> Any:
    manifest = _hydrate(control)
    control["manifest"] = manifest
    if internal_event:
        base, binding = control["base"], control["binding"]
        base.append_event(
            binding,
            ContinuityEvent("USER_TURN_COMMITTED", "internal-committed", NOW.isoformat()),
            expected_tail=base.tail(binding),
        )
    _stage(control, "Stop")
    assert control["lifecycle"].drain() == 2
    frozen = control["lifecycle"].pre_close(
        control["store"], _summary(control, manifest), context_digest=manifest, key="v3-close"
    )
    control["frozen"] = frozen
    return frozen


def _complete(control: dict[str, Any]) -> str:
    binding, request = control["binding"], control["frozen"].request_digest
    control["service"].compile_once(binding, request, **OWNER)
    control["service"].deliver_once(binding, request, **OWNER)
    _drain_runtime(control)
    result = control["service"].finalize(binding, request)
    assert isinstance(result, str)
    return result


CHILD = """
import json, os, signal, socket, sqlite3, sys
from contextlib import contextmanager
from pathlib import Path
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.local_continuity import ContinuityBinding
from zekam.domain.canonical import canonical_json, digest
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_control import SQLiteContinuityControlStore
def forbidden(*args, **kwargs):
    raise AssertionError('Control must not call providers/network')
socket.socket.connect = forbidden
socket.create_connection = forbidden
binding = ContinuityBinding(**json.loads(sys.argv[3]))
base = SQLiteContinuityStore(Path(sys.argv[1]))
spool = ClientLifecycleSpool(Path(sys.argv[2]), client_id=binding.client_id)
adapter = SQLiteContinuityControlStore(base, spool)
timing = sys.argv[4]
if timing != 'none':
    transaction = base._transaction
    @contextmanager
    def interrupted():
        with transaction() as db:
            yield db
            if timing == 'before-commit':
                os.kill(os.getpid(), signal.SIGKILL)
        os.kill(os.getpid(), signal.SIGKILL)
    base._transaction = interrupted
count = adapter.drain(binding)
with sqlite3.connect(sys.argv[1]) as db:
    db.row_factory = sqlite3.Row
    rows = [dict(row) for row in db.execute(
        'select * from continuity_control_event where session_id=? order by 1',
        (binding.session_id,))]
print(canonical_json({'count': count, 'rows_digest': digest(rows),
                     'report': adapter.inspect(binding)}))
"""


def _child(control: dict[str, Any], timing: str = "none") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            CHILD,
            str(control["base"].path),
            str(control["home"]),
            canonical_json(asdict(control["binding"])),
            timing,
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_completed_close_session_end_real_process_replay_and_no_generic_ack(
    control: dict[str, Any],
) -> None:
    _freeze(control)
    receipt = _complete(control)
    frozen_before = _frozen_rows(control)
    close_before = _rows(control, "close_receipt")
    entry = _stage(control, "SessionEnd")
    spool_before = _spool_bytes(control)
    assert control["lifecycle"].drain() == 3
    controls = _rows(control, "continuity_control_event")
    assert len(controls) == 1
    assert controls[0]["spool_digest"] == entry.entry_digest
    assert controls[0]["disposition"] == "advisory-post-close"
    report = control["lifecycle"].doctor()
    assert report["state"] == "healthy"
    assert report["session_state"] == "closed"
    assert report["ordinary_spool_count"] == 2
    assert report["control_event_count"] == 1
    assert report["persisted_spool_count"] == report["spool_event_count"] == 3
    assert report["grants_authority"] is report["generic_ack_created"] is False
    child = _child(control)
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    assert result["count"] == 3
    assert result["rows_digest"] == digest(controls)
    assert result["report"]["close_receipt_digest"] == receipt
    assert _frozen_rows(control) == frozen_before
    assert _rows(control, "close_receipt") == close_before
    assert _spool_bytes(control) == spool_before


def test_pending_advisory_never_finishes_close_then_replays_same_bytes(
    control: dict[str, Any],
) -> None:
    _freeze(control)
    _stage(control, "SessionEnd")
    assert control["lifecycle"].drain() == 3
    before = _rows(control, "continuity_control_event")
    report = control["lifecycle"].doctor()
    assert report["session_state"] == "closing"
    assert report["close_receipt_digest"] is None
    assert "pending-close" in report["issues"]
    assert _rows(control, "close_receipt") == []
    receipt = _complete(control)
    assert control["lifecycle"].drain() == 3
    assert _rows(control, "continuity_control_event") == before
    assert control["lifecycle"].doctor()["close_receipt_digest"] == receipt


def test_accepted_ordinary_exact_replay_never_becomes_control(control: dict[str, Any]) -> None:
    _freeze(control)
    _complete(control)
    before = _frozen_rows(control)
    assert control["lifecycle"].drain() == 2
    assert control["lifecycle"].drain() == 2
    assert _rows(control, "continuity_control_event") == []
    assert _frozen_rows(control) == before


def test_rejected_ordinary_is_durable_and_does_not_poison_later_advisory(
    control: dict[str, Any],
) -> None:
    _freeze(control)
    _complete(control)
    before = _frozen_rows(control)
    _stage(control, "SessionEnd")
    _stage(control, "Stop")
    _stage(control, "SessionEnd")
    spool_before = _spool_bytes(control)
    with pytest.raises(PolicyViolation, match="rejected after freeze"):
        control["lifecycle"].drain()
    rows = sorted(_rows(control, "continuity_control_event"), key=lambda row: row["spool_sequence"])
    assert [row["disposition"] for row in rows] == [
        "advisory-post-close",
        "rejected-after-freeze",
        "advisory-post-close",
    ]
    report = control["lifecycle"].doctor()
    assert report["persisted_spool_count"] == report["spool_event_count"] == 5
    assert report["rejected_count"] == 1
    assert report["issues"] == ["rejected-after-freeze"]
    assert report["state"] == "attention-required"
    with pytest.raises(PolicyViolation, match="rejected after freeze"):
        control["lifecycle"].drain()
    assert (
        sorted(_rows(control, "continuity_control_event"), key=lambda row: row["spool_sequence"])
        == rows
    )
    assert _spool_bytes(control) == spool_before
    assert _frozen_rows(control) == before


def test_internal_nonspooled_event_does_not_shift_frozen_spool_boundary(
    control: dict[str, Any],
) -> None:
    _freeze(control, internal_event=True)
    _stage(control, "SessionEnd")
    assert control["lifecycle"].drain() == 3
    report = control["controls"].inspect(control["binding"])
    assert report["event_count"] == 3
    assert report["ordinary_spool_count"] == 2
    assert report["control_event_count"] == 1
    assert _rows(control, "continuity_control_event")[0]["spool_sequence"] == 3


def test_historical_advisory_survives_source_drift_without_regranting_worker_authority(
    control: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze(control)
    monkeypatch.setattr(control["lifecycle"], "source_probe", lambda: digest("changed-source"))
    _stage(control, "SessionEnd")
    assert control["lifecycle"].drain() == 3
    report = control["lifecycle"].doctor()
    assert "current-source-or-authority-stale" in report["issues"]
    assert report["current_source_verified"] is False
    with pytest.raises(PolicyViolation, match="source"):
        control["service"].compile_once(
            control["binding"], control["frozen"].request_digest, **OWNER
        )
    with pytest.raises(PolicyViolation, match="source"):
        control["lifecycle"].hydrate(control["context"], key="must-not-hydrate-stale")
    assert _rows(control, "close_receipt") == []
    assert control["runtime"].status().claimed_outbox == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("internal_event_type", "pre_close"),
        ("external_event_type", "Stop"),
        ("client_version", "9999.0"),
        ("schema", "invented/v1"),
        ("client_kind", "opencode"),
        ("wire_digest", digest("forged")),
        ("reason", None),
    ],
)
def test_self_consistent_spool_digest_does_not_authorize_forged_parser_contract(
    control: dict[str, Any],
    field: str,
    value: Any,
) -> None:
    _freeze(control)
    binding = control["binding"]
    parsed = _parsed(binding.client_id, binding.external_session_id, "SessionEnd")
    observation = parsed.observation_body()
    observation[field] = value
    entry = control["spool"].stage(observation, delivery_id=digest((field, value)), occurred_at=NOW)
    entry.assert_integrity()  # structurally valid is deliberately not reviewed evidence
    with pytest.raises((PolicyViolation, ValidationFailed)):
        control["lifecycle"].drain()
    assert _rows(control, "continuity_control_event") == []


@pytest.mark.parametrize(
    "field",
    [
        "session_id",
        "external_session_id",
        "project_id",
        "realm_id",
        "client_id",
        "device_id",
        "source_snapshot_id",
        "work_item_id",
        "run_id",
        "plan_digest",
        "task_digest",
        "policy_digest",
    ],
)
def test_cross_scope_or_changed_historical_binding_never_records_control(
    control: dict[str, Any],
    field: str,
) -> None:
    _freeze(control)
    _stage(control, "SessionEnd")
    value = digest("wrong") if field.endswith("digest") else str(uuid4())
    wrong = replace(control["binding"], **{field: value})
    with pytest.raises((PolicyViolation, ValidationFailed)):
        control["controls"].drain(wrong)
    assert _rows(control, "continuity_control_event") == []


@pytest.mark.parametrize("method", ["is_frozen", "inspect", "drain"])
@pytest.mark.parametrize("value", [None, {}, [], True, ""])
def test_public_control_methods_reject_wrong_binding_types(
    control: dict[str, Any],
    method: str,
    value: Any,
) -> None:
    with pytest.raises(ValidationFailed):
        getattr(control["controls"], method)(value)
    assert _rows(control, "continuity_control_event") == []


@pytest.mark.parametrize("after_record", [False, True])
def test_missing_original_source_never_advances_or_validates_control(
    control: dict[str, Any],
    after_record: bool,
) -> None:
    _freeze(control)
    entry = _stage(control, "SessionEnd")
    if after_record:
        control["lifecycle"].drain()
    before = _rows(control, "continuity_control_event")
    source = control["spool"]._entry_path(entry.entry_digest)
    source.rename(source.with_suffix(".held"))  # only a disposable test artifact
    for method in (control["controls"].inspect, control["controls"].drain):
        with pytest.raises((PolicyViolation, ValidationFailed)):
            method(control["binding"])
    assert _rows(control, "continuity_control_event") == before


def test_corrupt_original_spool_bytes_are_not_hidden_by_existing_control_receipt(
    control: dict[str, Any],
) -> None:
    _freeze(control)
    entry = _stage(control, "SessionEnd")
    control["lifecycle"].drain()
    before = _rows(control, "continuity_control_event")
    control["spool"]._entry_path(entry.entry_digest).write_text("{}", encoding="utf-8")
    with pytest.raises((PolicyViolation, ValidationFailed)):
        control["controls"].inspect(control["binding"])
    with pytest.raises((PolicyViolation, ValidationFailed)):
        control["lifecycle"].drain()
    assert _rows(control, "continuity_control_event") == before


def test_concurrent_drains_publish_one_control_record(control: dict[str, Any]) -> None:
    _freeze(control)
    _stage(control, "SessionEnd")
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: control["lifecycle"].drain(), range(4)))
    assert results == [3] * 4
    assert len(_rows(control, "continuity_control_event")) == 1


@pytest.mark.skipif(os.name != "posix", reason="Real process SIGKILL proof is macOS/POSIX only")
@pytest.mark.parametrize("timing", ["before-commit", "after-commit"])
def test_process_death_at_control_commit_recovers_from_actual_spool(
    control: dict[str, Any],
    timing: str,
) -> None:
    _freeze(control)
    _stage(control, "SessionEnd")
    spool_before = _spool_bytes(control)
    child = _child(control, timing)
    assert child.returncode == -signal.SIGKILL, child.stderr
    rows = _rows(control, "continuity_control_event")
    assert len(rows) == (1 if timing == "after-commit" else 0)
    assert control["lifecycle"].drain() == 3
    result = _rows(control, "continuity_control_event")
    assert len(result) == 1
    if rows:
        assert result == rows
    assert _spool_bytes(control) == spool_before


def test_control_evidence_is_immutable_and_runtime_detects_tamper_after_guard_loss(
    control: dict[str, Any],
) -> None:
    _freeze(control)
    _stage(control, "SessionEnd")
    control["lifecycle"].drain()
    with sqlite3.connect(control["base"].path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("update continuity_control_event set observation_digest=?", (digest("bad"),))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("delete from continuity_control_event")
        db.execute("drop trigger continuity_control_event_no_update")
        db.execute("update continuity_control_event set observation_digest=?", (digest("bad"),))
    with pytest.raises(PolicyViolation, match="parity"):
        control["controls"].inspect(control["binding"])
    with pytest.raises(PolicyViolation, match="parity"):
        control["lifecycle"].drain()


def test_noncanonical_frozen_request_cannot_look_healthy_to_historical_control(
    control: dict[str, Any],
) -> None:
    _freeze(control)
    _stage(control, "SessionEnd")
    with sqlite3.connect(control["base"].path) as db:
        row = db.execute("select input_json from continuity_close_request").fetchone()
        db.execute("drop trigger continuity_close_request_immutable_update")
        db.execute(
            "update continuity_close_request set input_json=?", (json.dumps(json.loads(row[0])),)
        )
    with pytest.raises(PolicyViolation):
        control["lifecycle"].drain()
    assert _rows(control, "continuity_control_event") == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("checkpoint_digest", digest("foreign-checkpoint")),
        ("manifest_digest", digest("foreign-manifest")),
        ("outbox_id", "00000000-0000-4000-8000-000000000001"),
        ("projections_json", "[]"),
    ],
)
def test_corrupt_terminal_receipt_is_not_reported_as_healthy_control(
    control: dict[str, Any],
    field: str,
    value: str,
) -> None:
    _freeze(control)
    _complete(control)
    _stage(control, "SessionEnd")
    with sqlite3.connect(control["base"].path) as db:
        db.execute("drop trigger close_receipt_immutable_update")
        db.execute(f"update close_receipt set {field}=?", (value,))
    with pytest.raises(PolicyViolation):
        control["lifecycle"].drain()
    assert _rows(control, "continuity_control_event") == []


def test_corrupt_terminal_delivery_evidence_cannot_back_historical_close(
    control: dict[str, Any],
) -> None:
    _freeze(control)
    _complete(control)
    _stage(control, "SessionEnd")
    with sqlite3.connect(control["base"].path) as db:
        db.execute("drop trigger local_outbox_receipt_no_update")
        db.execute(
            "update local_outbox_receipt set evidence_digest=? where outbox_id=?",
            (digest("forged-delivery"), control["frozen"].outbox_id),
        )
    with pytest.raises(PolicyViolation):
        control["lifecycle"].drain()
    assert _rows(control, "continuity_control_event") == []


@pytest.mark.parametrize("recovery_kind", ["effect", "delivery"])
def test_control_historical_receipts_accept_explicitly_resolved_unknown_outcomes(
    control: dict[str, Any],
    recovery_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = _freeze(control)
    binding = control["binding"]
    if recovery_kind == "effect":
        original = control["files"].create_note
        calls = 0

        def partial(manifest: Any, payload: bytes) -> Path:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("Disposable partial note publication")
            return cast(Path, original(manifest, payload))

        monkeypatch.setattr(control["files"], "create_note", partial)
        with pytest.raises(OSError):
            control["service"].compile_once(binding, frozen.request_digest, **OWNER)
        monkeypatch.setattr(control["files"], "create_note", original)
        _stage(control, "SessionEnd")
        assert control["lifecycle"].drain() == 3
        before = _rows(control, "continuity_control_event")
        control["service"].repair_generated_candidates(
            binding, frozen.request_digest, repair_key="explicit-v3-test-repair", **OWNER
        )
        control["service"].deliver_once(binding, frozen.request_digest, **OWNER)
    else:
        control["service"].compile_once(binding, frozen.request_digest, **OWNER)
        claim = control["runtime"].claim_outbox(
            supported_kinds=("continuity.compile",),
            outbox_id=frozen.outbox_id,
            require_completed_job=True,
            lease_seconds=30,
            **OWNER,
        )
        assert claim is not None
        control["runtime"].record_outbox_receipt(
            claim, status="unknown", evidence_digest=digest("lost-response")
        )
        _stage(control, "SessionEnd")
        assert control["lifecycle"].drain() == 3
        before = _rows(control, "continuity_control_event")
        control["service"].reconcile_delivery(binding, frozen.request_digest)
    _drain_runtime(control)
    receipt = control["service"].finalize(binding, frozen.request_digest)
    assert control["lifecycle"].drain() == 3
    assert _rows(control, "continuity_control_event") == before
    assert control["lifecycle"].doctor()["close_receipt_digest"] == receipt
    child = _child(control)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout)["report"]["state"] == "healthy"


def test_historical_advisory_does_not_rewrite_changed_generated_note(
    control: dict[str, Any],
) -> None:
    frozen = _freeze(control)
    _complete(control)
    before = _frozen_rows(control)
    projection = frozen.projections(control["binding"])[0]
    path = control["home"] / projection.manifest.portable_ref
    replacement = b"User changed this disposable generated projection after terminal close.\n"
    path.write_bytes(replacement)
    _stage(control, "SessionEnd")
    assert control["lifecycle"].drain() == 3
    assert control["controls"].inspect(control["binding"])["state"] == "healthy"
    assert path.read_bytes() == replacement
    assert _frozen_rows(control) == before
    # Historical observation is not current projection validation or repair authority.
    with pytest.raises(PolicyViolation):
        control["service"].finalize(control["binding"], frozen.request_digest)
