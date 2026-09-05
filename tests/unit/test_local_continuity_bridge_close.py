"""Real command-hook parsers, disposable spool/DB, and bounded Akilli Kasa source.

These tests do not activate hooks or call providers. OpenCode's separate ledger is
not relabelled into a fabricated accepted command-hook observation.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity import NOW, ROOT, SOURCE_REF, _resolver
from tests.unit.test_local_continuity import continuity as continuity
from tests.unit.test_local_continuity_close import OWNER, _drain_runtime

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.home import HomeLayout
from zekam.application.knowledge_plane_service import KnowledgePlaneService
from zekam.application.local_continuity_close import CloseSummary, LocalCloseService
from zekam.application.local_continuity_service import LocalLifecycleContinuity
from zekam.application.opencode_lifecycle import OpenCodeForwardEvent, record_event
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.claude_lifecycle import (
    CLAUDE_CLIENT_ID,
    CLAUDE_REVIEWED_VERSION,
    parse_claude_hook_input,
)
from zekam.infrastructure.clients.codex_lifecycle import parse_codex_hook_input
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_close import SQLiteCloseStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

CLIENTS = ("codex", CLAUDE_CLIENT_ID)


@pytest.fixture(params=CLIENTS)
def bridge(
    continuity: Any,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    def no_network(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Continuity flow unexpectedly attempted a network/provider connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    base, binding, context = continuity
    if request.param != binding.client_id:
        binding = replace(
            binding,
            session_id=str(uuid4()),
            external_session_id=str(uuid4()),
            client_id=request.param,
        )
        base.bind_session(binding)
    home = tmp_path / "bridge-home"
    HomeLayout(home).ensure().ensure_project("akilli-kasa")
    spool = ClientLifecycleSpool(home, client_id=binding.client_id)

    def source_digest() -> str:
        return digest((ROOT / SOURCE_REF).read_text())

    lifecycle = LocalLifecycleContinuity(base, spool, binding, source_probe=source_digest)

    def probe(value: Any) -> None:
        if value != binding:
            raise PolicyViolation("Close owner does not match bridge owner")
        lifecycle.assert_current_source()

    runtime = SQLiteLocalRuntimeStore(base.path)
    files = KnowledgeFileStore(home)
    close_store = SQLiteCloseStore(base, runtime, files, source_probe=probe)
    close_service = LocalCloseService(
        close_store,
        runtime,
        KnowledgePlaneService(SQLiteOperationalStore(base.path), files),
        source_probe=probe,
        verify_projection=close_store.verify_projection,
    )
    return {
        "base": base,
        "binding": binding,
        "context": context,
        "home": home,
        "spool": spool,
        "lifecycle": lifecycle,
        "runtime": runtime,
        "files": files,
        "store": close_store,
        "service": close_service,
        "source_digest": source_digest(),
    }


def _parsed(client_id: str, session_id: str, event: str) -> Any:
    document: dict[str, Any] = {"session_id": session_id, "hook_event_name": event}
    if event == "SessionStart":
        document.update(source="startup", permission_mode="default")
    elif event in {"PreCompact", "PostCompact"}:
        document["trigger"] = "manual"
        if client_id == "codex":
            document["turn_id"] = str(uuid4())
    elif event == "Stop":
        document.update(stop_hook_active=False, permission_mode="default")
        if client_id == "codex":
            document["turn_id"] = str(uuid4())
    elif event == "SessionEnd":
        document["reason"] = "other"
    parser = parse_codex_hook_input if client_id == "codex" else parse_claude_hook_input
    return parser(json.dumps(document))


def _stage(bridge: dict[str, Any], event: str) -> Any:
    binding = bridge["binding"]
    parsed = _parsed(binding.client_id, binding.external_session_id, event)
    options = {"occurrence_id": str(uuid4())}
    if binding.client_id == CLAUDE_CLIENT_ID:
        options["client_version"] = CLAUDE_REVIEWED_VERSION
    return bridge["spool"].stage(
        parsed.observation_body(), delivery_id=parsed.delivery_id(**options), occurred_at=NOW
    )


def _hydrate(bridge: dict[str, Any], *, start: bool = True) -> str:
    if start:
        _stage(bridge, "SessionStart")
        assert bridge["lifecycle"].drain() == 1
    return cast(str, bridge["lifecycle"].hydrate(bridge["context"], key="bridge-hydrate"))


def _summary(bridge: dict[str, Any], manifest: str) -> CloseSummary:
    return CloseSummary(
        ("Inspected the bounded Akilli Kasa health endpoint source.",),
        (),
        (),
        ("Continue acceptance testing; do not activate hooks.",),
        "Verify the next approved acceptance gate.",
        ((SOURCE_REF, bridge["source_digest"]),),
        ((f"context/{manifest[7:]}", manifest),),
    )


def _no_close(bridge: dict[str, Any]) -> None:
    with sqlite3.connect(bridge["base"].path) as db:
        assert db.execute("select count(*) from continuity_close_request").fetchone()[0] == 0
        assert db.execute("select count(*) from close_receipt").fetchone()[0] == 0
        assert db.execute("select count(*) from local_job").fetchone()[0] == 0
        assert db.execute("select count(*) from local_outbox").fetchone()[0] == 0
        assert (
            db.execute(
                "select status from session where id=?", (bridge["binding"].session_id,)
            ).fetchone()[0]
            == "open"
        )


def test_real_parser_spool_checkpoint_process_resume_and_final_close(
    bridge: dict[str, Any],
) -> None:
    lifecycle = bridge["lifecycle"]
    binding = bridge["binding"]
    manifest = _hydrate(bridge)
    _stage(bridge, "PreCompact")
    assert lifecycle.drain() == 2
    checkpoint = lifecycle.pre_compaction(context_digest=manifest, key="before-compaction")
    before = bridge["base"].resume(binding, checkpoint)
    assert before["grants_authority"] is before["approval_inherited"] is False
    assert before["reacquire_required"] is True
    assert before["uncovered_events"] == 0

    # A genuinely separate interpreter reopens the on-disk state and actual source.
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json, socket, sys
from pathlib import Path
from tests.unit.test_local_continuity import ROOT, SOURCE_REF, _resolver
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_service import LocalLifecycleContinuity
from zekam.domain.canonical import canonical_json, digest
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
def forbidden(*args, **kwargs):
    raise AssertionError('No provider or network is allowed')
socket.socket.connect = forbidden
socket.create_connection = forbidden
binding = ContinuityBinding(**json.loads(sys.argv[3]))
store = SQLiteContinuityStore(Path(sys.argv[1]), source_resolver=_resolver(binding))
resume = store.resume(binding, sys.argv[4])
actual = (ROOT / SOURCE_REF).read_text()
assert resume['context']['context']['fragments'] == {'health-source': actual}
assert resume['grants_authority'] is resume['approval_inherited'] is False
assert resume['reacquire_required'] is True and resume['uncovered_events'] == 0
spool = ClientLifecycleSpool(Path(sys.argv[2]), client_id=binding.client_id)
service = LocalLifecycleContinuity(store, spool, binding, source_probe=lambda: digest(actual))
report = service.doctor()
assert report['state'] == 'healthy' and report['persisted_spool_count'] == 2
print(canonical_json({'resume_digest': digest(resume), 'source_digest': digest(actual)}))
""",
            str(bridge["base"].path),
            str(bridge["home"]),
            canonical_json(asdict(binding)),
            checkpoint,
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(child.stdout) == {
        "resume_digest": digest(before),
        "source_digest": bridge["source_digest"],
    }
    reopened = SQLiteContinuityStore(bridge["base"].path, source_resolver=_resolver(binding))
    assert reopened.resume(binding, checkpoint) == before
    _stage(bridge, "PostCompact")
    assert lifecycle.drain() == 3
    stop = _stage(bridge, "Stop")
    assert stop.internal_event_type == "pre_close"
    assert lifecycle.drain() == 4
    summary = _summary(bridge, manifest)
    frozen = lifecycle.pre_close(bridge["store"], summary, context_digest=manifest, key="close")
    assert (
        lifecycle.pre_close(bridge["store"], summary, context_digest=manifest, key="close")
        == frozen
    )
    assert bridge["runtime"].claim_next(**OWNER, lease_seconds=30) is None
    assert (
        bridge["service"].deliver_once(binding, frozen.request_digest, **OWNER).state == "pending"
    )
    bridge["service"].compile_once(binding, frozen.request_digest, **OWNER)
    bridge["service"].deliver_once(binding, frozen.request_digest, **OWNER)
    with pytest.raises(PolicyViolation, match="pending"):
        bridge["service"].finalize(binding, frozen.request_digest)
    _drain_runtime(bridge)
    receipt = bridge["service"].finalize(binding, frozen.request_digest)
    assert bridge["service"].finalize(binding, frozen.request_digest) == receipt
    assert (
        lifecycle.pre_close(bridge["store"], summary, context_digest=manifest, key="close").state
        == "complete"
    )
    assert lifecycle.drain() == 4  # exact replay never creates a new event
    report = lifecycle.doctor()
    assert report["state"] == "healthy"
    assert report["session_state"] == "closed"
    assert report["event_count"] == report["persisted_spool_count"] == 4
    assert report["close_receipt_digest"] == receipt
    assert digest((ROOT / SOURCE_REF).read_text()) == bridge["source_digest"]
    with sqlite3.connect(bridge["base"].path) as db:
        assert db.execute("select count(*) from close_receipt").fetchone()[0] == 1
        assert (
            db.execute("select state,authorship,materialized from knowledge_note").fetchall()
            == [("inbox", "generated", 1)] * 2
        )
    for projection in frozen.projections(binding):
        assert (
            bridge["home"] / projection.manifest.portable_ref
        ).read_bytes() == projection.payload


def test_missing_preclose_hook_never_creates_close(bridge: dict[str, Any]) -> None:
    manifest = _hydrate(bridge)
    with pytest.raises(PolicyViolation, match="PRE_CLOSE hook evidence missing"):
        bridge["lifecycle"].pre_close(
            bridge["store"], _summary(bridge, manifest), context_digest=manifest, key="close"
        )
    _no_close(bridge)


@pytest.mark.parametrize("event", ["PreCompact", "Stop"])
def test_unpersisted_spool_delta_blocks_checkpoint_or_close_without_ack(
    bridge: dict[str, Any], event: str
) -> None:
    manifest = _hydrate(bridge)
    entry = _stage(bridge, event)
    assert "unpersisted-spool-delta" in bridge["lifecycle"].doctor()["issues"]
    with pytest.raises(PolicyViolation, match="spool"):
        if event == "PreCompact":
            bridge["lifecycle"].pre_compaction(context_digest=manifest, key="undrained")
        else:
            bridge["lifecycle"].pre_close(
                bridge["store"],
                _summary(bridge, manifest),
                context_digest=manifest,
                key="undrained",
            )
    with sqlite3.connect(bridge["base"].path) as db:
        assert db.execute("select count(*) from continuity_checkpoint").fetchone()[0] == 0
    assert entry.entry_digest not in bridge["base"].spool_digests(bridge["binding"])
    assert not tuple(bridge["spool"].acks_directory.glob("*.json"))
    _no_close(bridge)


def test_session_end_is_advisory_and_not_a_preclose_authorization(bridge: dict[str, Any]) -> None:
    manifest = _hydrate(bridge)
    end = _stage(bridge, "SessionEnd")
    assert end.internal_event_type == "post_close"
    assert bridge["lifecycle"].drain() == 2
    with pytest.raises(PolicyViolation, match="PRE_CLOSE hook evidence missing"):
        bridge["lifecycle"].pre_close(
            bridge["store"], _summary(bridge, manifest), context_digest=manifest, key="close"
        )
    _no_close(bridge)


def test_delivery_replay_is_exact_and_drift_does_not_mutate_spool(bridge: dict[str, Any]) -> None:
    first = _stage(bridge, "SessionStart")
    assert (
        bridge["spool"].stage(first.observation, delivery_id=first.delivery_id, occurred_at=NOW)
        == first
    )
    foreign = _parsed(bridge["binding"].client_id, str(uuid4()), "SessionStart")
    with pytest.raises(PolicyViolation, match="replay payload drift"):
        bridge["spool"].stage(
            foreign.observation_body(), delivery_id=first.delivery_id, occurred_at=NOW
        )
    assert bridge["lifecycle"].drain() == 1
    assert bridge["lifecycle"].drain() == 1
    assert bridge["base"].inspect(bridge["binding"])["event_count"] == 1
    _no_close(bridge)


def test_foreign_client_or_session_event_never_enters_binding(bridge: dict[str, Any]) -> None:
    foreign_client = CLIENTS[1] if bridge["binding"].client_id == CLIENTS[0] else CLIENTS[0]
    foreign = _parsed(foreign_client, bridge["binding"].external_session_id, "Stop")
    entry = bridge["spool"].stage(
        foreign.observation_body(), delivery_id=digest("foreign-delivery"), occurred_at=NOW
    )
    with pytest.raises(PolicyViolation, match="external session/client mismatch"):
        bridge["lifecycle"]._event(entry)
    assert bridge["lifecycle"].drain() == 0
    other_session = _parsed(bridge["binding"].client_id, str(uuid4()), "Stop")
    other_entry = bridge["spool"].stage(
        other_session.observation_body(), delivery_id=digest("other-session"), occurred_at=NOW
    )
    with pytest.raises(PolicyViolation, match="external session/client mismatch"):
        bridge["lifecycle"]._event(other_entry)
    assert bridge["lifecycle"].drain() == 0
    assert bridge["base"].inspect(bridge["binding"])["event_count"] == 0
    _no_close(bridge)


def test_missing_session_start_cannot_silently_freeze_close(bridge: dict[str, Any]) -> None:
    manifest = _hydrate(bridge, start=False)
    _stage(bridge, "Stop")
    assert bridge["lifecycle"].drain() == 1
    with pytest.raises(PolicyViolation, match=r"SESSION_START|hook evidence|session start"):
        bridge["lifecycle"].pre_close(
            bridge["store"], _summary(bridge, manifest), context_digest=manifest, key="close"
        )
    _no_close(bridge)


def test_close_replay_cannot_ignore_new_unpersisted_spool_delta(bridge: dict[str, Any]) -> None:
    manifest = _hydrate(bridge)
    _stage(bridge, "Stop")
    bridge["lifecycle"].drain()
    summary = _summary(bridge, manifest)
    frozen = bridge["lifecycle"].pre_close(
        bridge["store"], summary, context_digest=manifest, key="close"
    )
    # Another real hook occurrence is not the same immutable delivery replay.
    _stage(bridge, "Stop")
    assert "unpersisted-spool-delta" in bridge["lifecycle"].doctor()["issues"]
    with pytest.raises(PolicyViolation, match=r"spool|delta|frozen"):
        bridge["lifecycle"].pre_close(
            bridge["store"], summary, context_digest=manifest, key="close"
        )
    assert bridge["store"].load(bridge["binding"], frozen.request_digest).state == "pending"
    with sqlite3.connect(bridge["base"].path) as db:
        assert db.execute("select count(*) from close_receipt").fetchone()[0] == 0
        assert db.execute("select count(*) from continuity_close_request").fetchone()[0] == 1


def test_real_opencode_ledger_is_not_fabricated_as_command_hook_observation(tmp_path: Path) -> None:
    event = record_event(
        tmp_path / "opencode-home", event_type="session.created", session_id="ses_bounded", now=NOW
    )
    captured = OpenCodeForwardEvent.capture(event.document())
    assert captured is not None
    spool = ClientLifecycleSpool(tmp_path / "spool-home", client_id="opencode")
    with pytest.raises(ValidationFailed, match="exact content-free schema"):
        spool.stage(event.document(), delivery_id=digest("opencode-delivery"), occurred_at=NOW)
    assert not spool.events_directory.exists()
