"""Independent publisher-only filesystem and durable-receipt safety regressions."""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from zekam.application.local_runtime import LocalOutboxClaim, LocalOutboxEvent
from zekam.application.local_runtime_service import (
    LocalEffectRequest,
    LocalEffectResult,
    LocalRuntimeService,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ZekamError
from zekam.infrastructure.local_runtime_effects import (
    LocalJournalEffectExecutor,
    LocalJournalOutboxPublisher,
)
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Publisher requires POSIX nofollow/flock")


def _claim() -> LocalOutboxClaim:
    payload = {"job_id": "job-review"}
    return LocalOutboxClaim(
        LocalOutboxEvent(
            "outbox-review",
            "job-review",
            "job:review:enqueued",
            "job.enqueued",
            digest(payload),
            payload,
            "claimed",
        ),
        "claim-review",
        "review-owner",
        os.getpid(),
        "review-token",
        1,
        "2099-01-01T00:00:00+00:00",
    )


def _record(claim: LocalOutboxClaim) -> bytes:
    return f"{claim.event.idempotency_key}\t{claim.event.payload_digest}\n".encode()


def _publisher(tmp_path: Path) -> tuple[Path, LocalJournalOutboxPublisher]:
    root = tmp_path / "runtime" / "local-effects"
    root.mkdir(parents=True, mode=0o700)
    return root, LocalJournalOutboxPublisher(root)


def test_publisher_preserves_existing_record_and_receipt_contract(tmp_path: Path) -> None:
    root, publisher = _publisher(tmp_path)
    claim = _claim()
    result = publisher(claim)
    assert (root / "outbox-delivery.journal").read_bytes() == _record(claim)
    assert result.status == "delivered"
    assert result.evidence_digest == digest(
        {
            "idempotency_key": claim.event.idempotency_key,
            "payload_digest": claim.event.payload_digest,
        }
    )


def test_missing_root_is_created_privately_without_changing_existing_ancestor(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o750)
    before = existing.stat()
    root = existing / "runtime" / "local-effects"
    publisher = LocalJournalOutboxPublisher(root)
    assert not root.exists()
    assert publisher(_claim()).status == "delivered"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(root.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(existing.stat().st_mode) == stat.S_IMODE(before.st_mode)
    assert existing.stat().st_ino == before.st_ino


def test_leaf_symlink_swap_never_appends_to_outside_victim(tmp_path: Path) -> None:
    root, publisher = _publisher(tmp_path)
    victim = tmp_path / "outside-victim"
    victim.write_bytes(b"preserve exact outside bytes\n")
    (root / "outbox-delivery.journal").symlink_to(victim)
    with pytest.raises((ZekamError, OSError)):
        publisher(_claim())
    assert victim.read_bytes() == b"preserve exact outside bytes\n"


def test_parent_symlink_swap_never_creates_outside_journal(tmp_path: Path) -> None:
    root, publisher = _publisher(tmp_path)
    victim = tmp_path / "outside-directory"
    victim.mkdir()
    marker = victim / "outbox-delivery.journal"
    marker.write_bytes(b"preserve parent victim\n")
    root.rmdir()
    root.symlink_to(victim, target_is_directory=True)
    with pytest.raises((ZekamError, OSError)):
        publisher(_claim())
    assert marker.read_bytes() == b"preserve parent victim\n"


def test_existing_ancestor_replacement_is_not_new_authority(tmp_path: Path) -> None:
    root, publisher = _publisher(tmp_path)
    retained = tmp_path / "retained-runtime"
    root.parent.rename(retained)
    root.mkdir(parents=True)
    with pytest.raises((ZekamError, OSError)):
        publisher(_claim())
    assert not (root / "outbox-delivery.journal").exists()
    assert not (retained / "local-effects" / "outbox-delivery.journal").exists()


def test_hardlink_never_mutates_outside_victim(tmp_path: Path) -> None:
    root, publisher = _publisher(tmp_path)
    victim = tmp_path / "hardlink-victim"
    victim.write_bytes(b"single original record\n")
    os.link(victim, root / "outbox-delivery.journal")
    with pytest.raises((ZekamError, OSError)):
        publisher(_claim())
    assert victim.read_bytes() == b"single original record\n"


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_nonregular_leaf_fails_without_blocking(tmp_path: Path, kind: str) -> None:
    root, publisher = _publisher(tmp_path)
    target = root / "outbox-delivery.journal"
    if kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target, mode=0o600)
    with pytest.raises((ZekamError, OSError)):
        publisher(_claim())


def test_short_writes_complete_exact_single_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, publisher = _publisher(tmp_path)
    original = os.write
    calls = 0

    def short_write(fd: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        return original(fd, data[: max(1, len(data) // 3)])

    monkeypatch.setattr(os, "write", short_write)
    assert publisher(_claim()).status == "delivered"
    assert calls > 1
    assert (root / "outbox-delivery.journal").read_bytes() == _record(_claim())


def test_no_progress_write_never_returns_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, publisher = _publisher(tmp_path)
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)
    with pytest.raises((ZekamError, OSError)):
        publisher(_claim())


def test_readback_mismatch_never_returns_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, publisher = _publisher(tmp_path)
    monkeypatch.setattr(os, "pread", lambda _fd, size, _offset: b"x" * size)
    with pytest.raises((ZekamError, OSError)):
        publisher(_claim())


def test_directory_and_file_are_synced_before_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime" / "local-effects"
    publisher = LocalJournalOutboxPublisher(root)
    original = os.fsync
    kinds: list[str] = []

    def sync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        original(fd)

    monkeypatch.setattr(os, "fsync", sync)
    assert publisher(_claim()).status == "delivered"
    assert "file" in kinds
    assert kinds.count("directory") >= 3
    assert kinds[-1] == "directory"


def test_busy_journal_is_not_written_and_can_be_used_after_lock_release(tmp_path: Path) -> None:
    import fcntl

    root, publisher = _publisher(tmp_path)
    target = root / "outbox-delivery.journal"
    target.write_bytes(b"prior\n")
    with target.open("rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((ZekamError, OSError)):
            publisher(_claim())
        assert target.read_bytes() == b"prior\n"
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    assert publisher(_claim()).status == "delivered"
    assert target.read_bytes() == b"prior\n" + _record(_claim())


def test_leaf_swap_after_write_is_not_reported_as_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, publisher = _publisher(tmp_path)
    victim = tmp_path / "postwrite-victim"
    victim.write_bytes(b"unchanged\n")
    target = root / "outbox-delivery.journal"
    original = os.write
    swapped = False

    def write_and_swap(fd: int, data: bytes) -> int:
        nonlocal swapped
        result = original(fd, data)
        if not swapped:
            swapped = True
            target.unlink()
            target.symlink_to(victim)
        return result

    monkeypatch.setattr(os, "write", write_and_swap)
    with pytest.raises((ZekamError, OSError)):
        publisher(_claim())
    assert victim.read_bytes() == b"unchanged\n"


def test_journal_limit_rejects_before_append(tmp_path: Path) -> None:
    root, publisher = _publisher(tmp_path)
    target = root / "outbox-delivery.journal"
    with target.open("wb") as stream:
        stream.truncate(256 * 1024 * 1024)
    before = target.stat().st_size
    with pytest.raises((ZekamError, OSError)):
        publisher(_claim())
    assert target.stat().st_size == before


def test_record_limit_rejects_before_file_creation(tmp_path: Path) -> None:
    root, publisher = _publisher(tmp_path)
    claim = _claim()
    oversized = replace(claim, event=replace(claim.event, idempotency_key="x" * 16_384))
    with pytest.raises((ZekamError, OSError)):
        publisher(oversized)
    assert not (root / "outbox-delivery.journal").exists()


def test_wrong_owner_leaf_is_rejected_before_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, publisher = _publisher(tmp_path)
    target = root / "outbox-delivery.journal"
    target.write_bytes(b"preserve ownership victim\n")
    identity = target.stat().st_ino
    original = os.fstat

    def foreign_owner(fd: int) -> os.stat_result:
        actual = original(fd)
        if actual.st_ino != identity:
            return actual
        fields = list(actual)
        fields[4] = os.getuid() + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises((ZekamError, OSError)):
        publisher(_claim())
    assert target.read_bytes() == b"preserve ownership victim\n"


def test_partial_write_error_never_returns_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, publisher = _publisher(tmp_path)
    original = os.write
    calls = 0

    def partial_then_fail(fd: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("injected partial-write ambiguity")
        return original(fd, data[:3])

    monkeypatch.setattr(os, "write", partial_then_fail)
    with pytest.raises((ZekamError, OSError)):
        publisher(_claim())
    assert (root / "outbox-delivery.journal").read_bytes() == _record(_claim())[:3]


def test_directory_sync_error_never_returns_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, publisher = _publisher(tmp_path)
    original = os.fsync

    def fail_directory_sync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory-entry durability ambiguity")
        original(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    with pytest.raises((ZekamError, OSError)):
        publisher(_claim())


def test_file_sync_failure_records_unknown_and_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, publisher = _publisher(tmp_path)
    store = SQLiteLocalRuntimeStore(tmp_path / "operational.db")
    store.enqueue(idempotency_key="journal-review", payload={"operation": "test", "effect": {}})
    service = LocalRuntimeService(
        store,
        effect_executor=lambda _: LocalEffectResult("completed", digest("unused")),
        outbox_publisher=publisher,
    )
    original = os.fsync

    def fail_file_sync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("injected durable write ambiguity")
        original(fd)

    with monkeypatch.context() as local:
        local.setattr(os, "fsync", fail_file_sync)
        claim = service.publish_outbox_once(
            owner_id="review",
            owner_pid=os.getpid(),
            owner_token="review-token",
        )
    assert claim is not None
    written = (root / "outbox-delivery.journal").read_bytes()
    assert written == _record(claim)
    assert store.status().recovery_outbox == 1
    assert store.status().open_recovery_cases == 1
    assert (
        service.publish_outbox_once(
            owner_id="review",
            owner_pid=os.getpid(),
            owner_token="review-token",
        )
        is None
    )
    assert (root / "outbox-delivery.journal").read_bytes() == written


def _effect(relative: str = "events.log", line: str = "effect record") -> LocalEffectRequest:
    return LocalEffectRequest(
        "local.append-journal/v1",
        "effect-review-key",
        {"relative_path": relative, "line": line},
    )


def test_effect_nested_relative_path_preserves_tsv_and_receipt_contract(tmp_path: Path) -> None:
    root = tmp_path / "runtime" / "local-effects"
    executor = LocalJournalEffectExecutor(root)
    assert not root.exists()
    request = _effect("nested/child/events.log")
    result = executor(request)
    assert result.status == "completed"
    assert result.evidence_digest == digest(
        {
            "idempotency_key": request.idempotency_key,
            "line": request.payload["line"],
        }
    )
    assert (root / "nested/child/events.log").read_bytes() == b"effect-review-key\teffect record\n"


def test_effect_does_not_inherit_smaller_publisher_record_limit(tmp_path: Path) -> None:
    executor = LocalJournalEffectExecutor(tmp_path)
    line = "x" * (512 * 1024)
    result = executor(_effect(line=line))
    assert result.status == "completed"
    assert (tmp_path / "events.log").read_bytes() == b"effect-review-key\t" + line.encode() + b"\n"


def test_effect_hardlink_never_mutates_outside_victim(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    victim = tmp_path / "effect-hardlink-victim"
    victim.write_bytes(b"preserve effect victim\n")
    os.link(victim, root / "events.log")
    executor = LocalJournalEffectExecutor(root)
    with pytest.raises((ZekamError, OSError)):
        executor(_effect())
    assert victim.read_bytes() == b"preserve effect victim\n"


def test_effect_resolve_open_race_never_mutates_outside_victim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    executor = LocalJournalEffectExecutor(root)
    victim = tmp_path / "effect-race-victim"
    victim.write_bytes(b"preserve race victim\n")
    target = root / "events.log"
    original = os.open
    swapped = False

    def race_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if (
            os.fsdecode(path).endswith("events.log")
            and flags & (os.O_WRONLY | os.O_RDWR)
            and not swapped
        ):
            target.symlink_to(victim)
            swapped = True
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", race_open)
    with pytest.raises((ZekamError, OSError)):
        executor(_effect())
    assert swapped
    assert victim.read_bytes() == b"preserve race victim\n"


def test_effect_parent_swap_after_construction_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    executor = LocalJournalEffectExecutor(root)
    victim = tmp_path / "effect-parent-victim"
    victim.mkdir()
    marker = victim / "events.log"
    marker.write_bytes(b"preserve parent bytes\n")
    root.rmdir()
    root.symlink_to(victim, target_is_directory=True)
    with pytest.raises((ZekamError, OSError)):
        executor(_effect())
    assert marker.read_bytes() == b"preserve parent bytes\n"


def test_effect_nested_parent_symlink_does_not_escape_root(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    executor = LocalJournalEffectExecutor(root)
    victim = tmp_path / "nested-victim"
    victim.mkdir()
    (root / "nested").symlink_to(victim, target_is_directory=True)
    with pytest.raises((ZekamError, OSError)):
        executor(_effect("nested/events.log"))
    assert not (victim / "events.log").exists()


def test_effect_readback_mismatch_is_not_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = LocalJournalEffectExecutor(tmp_path)
    monkeypatch.setattr(os, "pread", lambda _fd, size, _offset: b"x" * size)
    with pytest.raises((ZekamError, OSError)):
        executor(_effect())


def test_effect_busy_file_does_not_append(tmp_path: Path) -> None:
    import fcntl

    target = tmp_path / "events.log"
    target.write_bytes(b"prior\n")
    executor = LocalJournalEffectExecutor(tmp_path)
    with target.open("rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((ZekamError, OSError)):
            executor(_effect())
    assert target.read_bytes() == b"prior\n"


def test_effect_file_sync_failure_is_unknown_without_implicit_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    executor = LocalJournalEffectExecutor(root)
    store = SQLiteLocalRuntimeStore(tmp_path / "operational.db")
    store.enqueue(
        idempotency_key="effect-review",
        payload={
            "operation": "local.append-journal/v1",
            "effect": {"relative_path": "events.log", "line": "once"},
        },
    )
    service = LocalRuntimeService(
        store,
        effect_executor=executor,
        outbox_publisher=LocalJournalOutboxPublisher(root),
    )
    original = os.fsync

    def fail_file_sync(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("injected effect durability ambiguity")
        original(fd)

    with monkeypatch.context() as local:
        local.setattr(os, "fsync", fail_file_sync)
        work = service.run_worker_once(
            owner_id="review",
            owner_pid=os.getpid(),
            owner_token="review-token",
        )
    assert work is not None
    first_bytes = (root / "events.log").read_bytes()
    assert first_bytes.endswith(b"\tonce\n") and first_bytes.count(b"\n") == 1
    assert store.status().recovery_jobs == 1
    assert store.status().open_recovery_cases == 1
    assert (
        service.run_worker_once(
            owner_id="review",
            owner_pid=os.getpid(),
            owner_token="review-token",
        )
        is None
    )
    assert (root / "events.log").read_bytes() == first_bytes
