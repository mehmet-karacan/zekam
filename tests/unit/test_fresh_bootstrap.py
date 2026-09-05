from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from zekam.application.fresh_bootstrap import (
    BACKUP_CONTRACT_RELATIVE_PATH,
    FAULT_POINTS,
    LOCK_SCHEMA,
    OPERATIONAL_RELATIVE_PATH,
    RECEIPT_RELATIVE_PATH,
    STAGE_MARKER_SCHEMA,
    FreshBootstrapPlan,
    detect_legacy_postgresql_config,
)
from zekam.application.fresh_bootstrap import (
    apply_fresh_bootstrap as _apply_fresh_bootstrap,
)
from zekam.application.fresh_bootstrap import (
    plan_fresh_bootstrap as _plan_fresh_bootstrap,
)
from zekam.application.home import HOME_ENTRIES, LAYOUT_SCHEMA
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.infrastructure.sqlite.operational_schema import SQLiteOperationalSchema
from zekam.infrastructure.sqlite.operational_schema import status as sqlite_status

AUTHORITY_DIGEST = "sha256:" + "a" * 64
OTHER_AUTHORITY_DIGEST = "sha256:" + "b" * 64
_SCHEMA = SQLiteOperationalSchema()


def plan_fresh_bootstrap(
    *, home: Path, core_root: Path, authority_digest: str
) -> FreshBootstrapPlan:
    return _plan_fresh_bootstrap(
        home=home,
        core_root=core_root,
        authority_digest=authority_digest,
        schema=_SCHEMA,
    )


def apply_fresh_bootstrap(
    plan: FreshBootstrapPlan, *, fault_at: str | None = None
) -> dict[str, object]:
    return _apply_fresh_bootstrap(plan, schema=_SCHEMA, fault_at=fault_at)


def _plan(tmp_path: Path) -> FreshBootstrapPlan:
    core = tmp_path / "core"
    core.mkdir(exist_ok=True)
    return plan_fresh_bootstrap(
        home=tmp_path / "home",
        core_root=core,
        authority_digest=AUTHORITY_DIGEST,
    )


def test_plan_is_read_only_and_exact(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    assert plan.action == "create"
    assert plan.plan_digest.startswith("sha256:")
    assert not plan.home.exists()
    assert plan.as_dict()["network_required"] is False
    assert plan.as_dict()["legacy_postgresql_data_used"] is False


@pytest.mark.parametrize("digest", ["", "sha256:no", "md5:" + "a" * 32])
def test_plan_rejects_noncanonical_authority_before_mutation(tmp_path: Path, digest: str) -> None:
    core = tmp_path / "core"
    core.mkdir()
    home = tmp_path / "home"

    with pytest.raises(ValidationFailed, match="Digest"):
        plan_fresh_bootstrap(home=home, core_root=core, authority_digest=digest)

    assert not home.exists()


def test_apply_publishes_complete_v2_home_and_zero_domain_rows(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    receipt = apply_fresh_bootstrap(plan)

    assert receipt["status"] == "completed"
    assert receipt["legacy_postgresql_data_used"] is False
    assert receipt["network_calls"] == receipt["docker_calls"] == 0
    for entry in HOME_ENTRIES:
        assert (plan.home / entry.relative).is_dir(), entry.relative
    layout = json.loads((plan.home / "layout.json").read_text(encoding="utf-8"))
    assert layout["schema"] == LAYOUT_SCHEMA
    assert (plan.home / RECEIPT_RELATIVE_PATH).is_file()
    assert (plan.home / BACKUP_CONTRACT_RELATIVE_PATH).is_file()
    database = plan.home / OPERATIONAL_RELATIVE_PATH
    assert sqlite_status(database).integrity_ok
    with sqlite3.connect(database) as connection:
        counts = [
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in ("project", "work_item", "run", "session", "model_identity")
        ]
        knowledge_tables = connection.execute(
            "select count(*) from sqlite_master where type = 'table' "
            "and name in ('knowledge_chunk', 'knowledge_embedding')"
        ).fetchone()[0]
    assert counts == [0, 0, 0, 0, 0]
    assert knowledge_tables == 0


def test_second_apply_is_idempotent_and_preserves_user_content(tmp_path: Path) -> None:
    first_plan = _plan(tmp_path)
    first = apply_fresh_bootstrap(first_plan)
    marker = first_plan.home / "global" / "notlarim.txt"
    marker.write_text("kullanici", encoding="utf-8")

    replay_plan = plan_fresh_bootstrap(
        home=first_plan.home,
        core_root=first_plan.core_root,
        authority_digest=AUTHORITY_DIGEST,
    )
    replay = apply_fresh_bootstrap(replay_plan)

    assert replay_plan.action == "already-initialized"
    assert replay["receipt_digest"] == first["receipt_digest"]
    assert marker.read_text(encoding="utf-8") == "kullanici"


@pytest.mark.parametrize("fault_at", sorted(FAULT_POINTS))
def test_every_fault_point_leaves_no_partial_published_home(tmp_path: Path, fault_at: str) -> None:
    plan = _plan(tmp_path)

    with pytest.raises(OSError, match="injected"):
        apply_fresh_bootstrap(plan, fault_at=fault_at)

    assert not plan.home.exists()
    assert not (plan.home.parent / f".{plan.home.name}.bootstrap.lock").exists()
    assert not list(plan.home.parent.glob(f".{plan.home.name}.bootstrap-*"))


def test_unknown_fault_point_is_rejected_before_mutation(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    with pytest.raises(ConfigurationError, match="Bilinmeyen"):
        apply_fresh_bootstrap(plan, fault_at="typo")
    assert not plan.home.exists()


def test_legacy_postgresql_config_blocks_without_connection(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan.home.mkdir()
    (plan.home / "config.yaml").write_text(
        "schema: zekam-config/v1\ndatabase:\n  backend: postgresql\n  host: legacy\n",
        encoding="utf-8",
    )

    detection = detect_legacy_postgresql_config(plan.home)
    assert detection.detected is True
    assert "database.backend=postgresql" in detection.reasons
    with pytest.raises(ConfigurationError, match="baglanti kurulmadan"):
        plan_fresh_bootstrap(
            home=plan.home,
            core_root=plan.core_root,
            authority_digest=AUTHORITY_DIGEST,
        )


def test_duplicate_yaml_cannot_hide_legacy_postgresql_backend(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan.home.mkdir()
    (plan.home / "config.yaml").write_text(
        "database:\n  backend: postgresql\ndatabase:\n  backend: sqlite\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="guvenle okunamadi"):
        detect_legacy_postgresql_config(plan.home)


def test_symlink_parent_cannot_publish_home_inside_core(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(core, target_is_directory=True)

    with pytest.raises(ConfigurationError, match="core source agacinin icinde"):
        plan_fresh_bootstrap(
            home=alias / "hidden-home",
            core_root=core,
            authority_digest=AUTHORITY_DIGEST,
        )

    assert not (core / "hidden-home").exists()


def test_corrupt_receipt_fails_closed_without_repairing_user_home(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    apply_fresh_bootstrap(plan)
    receipt = plan.home / RECEIPT_RELATIVE_PATH
    receipt.write_text("{}", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="receipt"):
        plan_fresh_bootstrap(
            home=plan.home,
            core_root=plan.core_root,
            authority_digest=AUTHORITY_DIGEST,
        )
    assert receipt.read_text(encoding="utf-8") == "{}"


def test_duplicate_json_key_receipt_is_rejected_even_when_last_value_and_digest_are_valid(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    apply_fresh_bootstrap(plan)
    receipt = plan.home / RECEIPT_RELATIVE_PATH
    raw = receipt.read_text(encoding="utf-8")
    raw = raw.replace(
        '"operational_engine": "cpython-sqlite"',
        '"operational_engine": "postgresql",\n  "operational_engine": "cpython-sqlite"',
    )
    receipt.write_text(raw, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="receipt okunamadi"):
        plan_fresh_bootstrap(
            home=plan.home,
            core_root=plan.core_root,
            authority_digest=AUTHORITY_DIGEST,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operational_engine", "postgresql"),
        ("operational_schema_version", -1),
        ("operational_schema_version", True),
        ("initial_operational_rows", 999),
        ("legacy_postgresql_data_used", True),
        ("network_calls", False),
        ("docker_calls", -1),
        ("plan_digest", "not-a-digest"),
        ("authority_digest", "not-a-digest"),
    ],
)
def test_semantically_forged_receipt_is_rejected_even_with_valid_self_digest(
    tmp_path: Path, field: str, value: object
) -> None:
    plan = _plan(tmp_path)
    apply_fresh_bootstrap(plan)
    receipt_path = plan.home / RECEIPT_RELATIVE_PATH
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    document[field] = value
    document.pop("receipt_digest")
    import hashlib

    canonical = json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    document["receipt_digest"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    receipt_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="receipt"):
        plan_fresh_bootstrap(
            home=plan.home,
            core_root=plan.core_root,
            authority_digest=AUTHORITY_DIGEST,
        )


def test_authority_drift_replay_fails_closed_and_preserves_user_content(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    apply_fresh_bootstrap(plan)
    marker = plan.home / "global" / "user.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="authority drift"):
        plan_fresh_bootstrap(
            home=plan.home,
            core_root=plan.core_root,
            authority_digest=OTHER_AUTHORITY_DIGEST,
        )

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_stale_owned_stage_is_quarantined_on_recovery(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    stale = plan.home.parent / f".{plan.home.name}.bootstrap-stale"
    stale.mkdir()
    (stale / ".bootstrap-stage.json").write_text(
        json.dumps(
            {
                "schema": STAGE_MARKER_SCHEMA,
                "home_name": plan.home.name,
                "plan_digest": plan.plan_digest,
            }
        ),
        encoding="utf-8",
    )
    (stale / "partial.db").write_bytes(b"partial")

    result = apply_fresh_bootstrap(plan)

    assert result["recovered_stages"] == [stale.name]
    recovered = plan.home.parent / f".{plan.home.name}.bootstrap-recovery" / stale.name
    assert (recovered / "partial.db").read_bytes() == b"partial"


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL/POSIX recovery assertion")
def test_process_kill_lock_and_partial_stage_are_recovered_on_restart(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    child_code = """
import json
import os
import signal
import sys
from pathlib import Path
from zekam.application.fresh_bootstrap import (
    STAGE_MARKER_SCHEMA,
    _acquire_bootstrap_lock,
    plan_fresh_bootstrap,
)
from zekam.infrastructure.sqlite.operational_schema import SQLiteOperationalSchema
home, core, authority = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
plan = plan_fresh_bootstrap(
    home=home,
    core_root=core,
    authority_digest=authority,
    schema=SQLiteOperationalSchema(),
)
lock = home.parent / f'.{home.name}.bootstrap.lock'
_acquire_bootstrap_lock(lock, plan)
stage = home.parent / f'.{home.name}.bootstrap-killed-child'
stage.mkdir()
(stage / '.bootstrap-stage.json').write_text(json.dumps({
    'schema': STAGE_MARKER_SCHEMA,
    'home_name': home.name,
    'plan_digest': plan.plan_digest,
}), encoding='utf-8')
(stage / 'partial.bin').write_bytes(b'partial-before-kill')
os.kill(os.getpid(), signal.SIGKILL)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            child_code,
            str(plan.home),
            str(plan.core_root),
            plan.authority_digest,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert child.returncode == -signal.SIGKILL
    result = apply_fresh_bootstrap(plan)

    assert result["status"] == "completed"
    assert result["recovered_stages"] == [f".{plan.home.name}.bootstrap-killed-child"]
    recovery = plan.home.parent / f".{plan.home.name}.bootstrap-recovery"
    recovered_stage = recovery / f".{plan.home.name}.bootstrap-killed-child"
    assert (recovered_stage / "partial.bin").read_bytes() == b"partial-before-kill"
    dead_lock_prefix = f".{plan.home.name}.bootstrap.lock.dead-"
    assert any(path.name.startswith(dead_lock_prefix) for path in recovery.iterdir())


def test_same_pid_orphan_lock_is_treated_as_prior_process_incarnation(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    lock = plan.home.parent / f".{plan.home.name}.bootstrap.lock"
    lock.write_text(
        json.dumps(
            {
                "schema": LOCK_SCHEMA,
                "home_name": plan.home.name,
                "plan_digest": plan.plan_digest,
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )

    result = apply_fresh_bootstrap(plan)

    assert result["status"] == "completed"
    recovery = plan.home.parent / f".{plan.home.name}.bootstrap-recovery"
    dead_lock_prefix = f".{plan.home.name}.bootstrap.lock.dead-"
    assert any(path.name.startswith(dead_lock_prefix) for path in recovery.iterdir())


def test_concurrent_apply_has_one_publish_and_retry_is_idempotent(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    def attempt() -> str:
        try:
            apply_fresh_bootstrap(plan)
        except ConfigurationError:
            return "locked-or-raced"
        return "published"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))

    assert results.count("published") == 1
    assert results.count("locked-or-raced") == 1
    replay = plan_fresh_bootstrap(
        home=plan.home,
        core_root=plan.core_root,
        authority_digest=AUTHORITY_DIGEST,
    )
    assert apply_fresh_bootstrap(replay)["status"] == "completed"


def test_same_process_link_registration_window_cannot_be_stolen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zekam.application import fresh_bootstrap as module

    plan = _plan(tmp_path)
    lock = plan.home.parent / f".{plan.home.name}.bootstrap.lock"
    linked = Event()
    release = Event()
    real_link = os.link

    def controlled_link(source: Path, destination: Path) -> None:
        real_link(source, destination)
        linked.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(os, "link", controlled_link)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(module._acquire_bootstrap_lock, lock, plan)
        assert linked.wait(timeout=5)
        second = executor.submit(module._acquire_bootstrap_lock, lock, plan)
        release.set()
        first.result(timeout=5)
        with pytest.raises(ConfigurationError, match="canli islem"):
            second.result(timeout=5)

    lock.unlink()
    with module._PROCESS_LOCK_GUARD:
        module._PROCESS_OWNED_LOCKS.discard(lock)


def test_same_process_release_window_cannot_erase_next_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zekam.application import fresh_bootstrap as module

    plan = _plan(tmp_path)
    lock = plan.home.parent / f".{plan.home.name}.bootstrap.lock"
    module._acquire_bootstrap_lock(lock, plan)
    unlinked = Event()
    release = Event()
    real_unlink = Path.unlink
    controlled_once = True

    def controlled_unlink(path: Path, *, missing_ok: bool = False) -> None:
        nonlocal controlled_once
        real_unlink(path, missing_ok=missing_ok)
        if path == lock and controlled_once:
            controlled_once = False
            unlinked.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(Path, "unlink", controlled_unlink)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_release = executor.submit(module._release_bootstrap_lock, lock)
        assert unlinked.wait(timeout=5)
        second_acquire = executor.submit(module._acquire_bootstrap_lock, lock, plan)
        release.set()
        first_release.result(timeout=5)
        second_acquire.result(timeout=5)

    with pytest.raises(ConfigurationError, match="canli islem"):
        module._acquire_bootstrap_lock(lock, plan)
    module._release_bootstrap_lock(lock)


def test_lock_parent_fsync_failure_uses_atomic_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zekam.application import fresh_bootstrap as module

    plan = _plan(tmp_path)
    lock = plan.home.parent / f".{plan.home.name}.bootstrap.lock"

    def fail_fsync(_path: Path) -> None:
        raise OSError("injected-parent-fsync")

    monkeypatch.setattr(module, "_fsync_directory", fail_fsync)
    with pytest.raises(OSError, match="injected-parent-fsync"):
        module._acquire_bootstrap_lock(lock, plan)

    assert not lock.exists()
    with module._PROCESS_LOCK_GUARD:
        assert lock not in module._PROCESS_OWNED_LOCKS


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission assertion")
def test_sensitive_bootstrap_files_are_owner_only(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    apply_fresh_bootstrap(plan)

    assert (plan.home / "config.yaml").stat().st_mode & 0o777 == 0o600
    assert (plan.home / OPERATIONAL_RELATIVE_PATH).stat().st_mode & 0o777 == 0o600
    assert (plan.home / "secrets").stat().st_mode & 0o777 == 0o700
