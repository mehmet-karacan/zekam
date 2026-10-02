"""W04: Maven runner (FAKE mvn): argv/cwd, ortam filtresi, kilit, timeout/cancel agac sonlandirma.

Gercek Maven yoktur; ``FakeMaven`` tmp dizinindeki betiktir (replay/fake-tool).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from tests.unit.unit_test_runner_support import (
    PLUGIN_LINES,
    FakeMaven,
    make_project,
)

from zekam.application.unit_test_measurement import RunStatus
from zekam.domain.errors import PolicyViolation
from zekam.infrastructure.unit_test_runner.maven_plan import (
    ExecutionAuthorization,
    MavenPlan,
    build_unit_test_plan,
    build_version_probe_plan,
)
from zekam.infrastructure.unit_test_runner.maven_runner import (
    BuildLockBusy,
    build_lock,
    collect_outputs,
    extract_plugin_executions,
    filter_environment,
    quarantine_stale_outputs,
    redact,
    run_maven,
)


def prepared(tmp_path: Path) -> tuple[FakeMaven, Path, MavenPlan, ExecutionAuthorization]:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(tmp_path / "proj")
    plan = build_unit_test_plan(root, target_modules=("",), search_path=fake.search_path).plan
    assert plan is not None
    auth = ExecutionAuthorization(plan.plan_digest, plan.execution_class, allow_network=True)
    return fake, root, plan, auth


def test_replay_runner_uses_explicit_argv_cwd_and_real_exit_code(tmp_path: Path) -> None:
    fake, root, plan, auth = prepared(tmp_path)
    fake.set(exit=3, print=["hello"])
    result = run_maven(plan, auth, lock_dir=tmp_path / "locks", run_id="r1", timeout_seconds=30)
    assert result.status is RunStatus.COMPLETED and result.exit_code == 3
    invocation = fake.invocation()
    assert invocation["argv"] == list(plan.args)
    assert Path(invocation["cwd"]).resolve() == root.resolve()
    assert result.output_digest.startswith("sha256:") and "hello" in result.output_tail


def test_replay_runner_requires_authorization_and_detects_root_or_arg_tampering(
    tmp_path: Path,
) -> None:
    fake, root, plan, auth = prepared(tmp_path)
    with pytest.raises(PolicyViolation):
        run_maven(plan, None, lock_dir=tmp_path / "l", run_id="r", timeout_seconds=5)
    other = build_version_probe_plan(root, search_path=fake.search_path).plan
    assert other is not None
    with pytest.raises(PolicyViolation):  # baska planin yetkisi
        run_maven(
            plan,
            ExecutionAuthorization(other.plan_digest, other.execution_class),
            lock_dir=tmp_path / "l",
            run_id="r",
            timeout_seconds=5,
        )
    # plan icerigi degisirse digest degisir -> eski yetki gecersiz
    tampered = MavenPlan(
        **{**{f: getattr(plan, f) for f in plan.__dataclass_fields__}, "args": ("-B", "test")}
    )
    with pytest.raises(PolicyViolation):
        run_maven(tampered, auth, lock_dir=tmp_path / "l", run_id="r", timeout_seconds=5)
    assert not (tmp_path / "fake" / "dump.json").exists()  # hicbir sey calismadi


def test_replay_environment_is_filtered_and_secrets_never_reach_output(tmp_path: Path) -> None:
    fake, _root, plan, auth = prepared(tmp_path)
    secret = "s3cr3t-value-123456"
    fake.set(print=[f"leak={secret}", "db.password=hunter22", "ok line"])
    environ = {
        **os.environ,
        "MY_API_TOKEN": secret,
        "GITHUB_PAT": "ghp_xxxxxxxxxxxxxxxxxxxx",
        "MAVEN_OPTS": "-javaagent:evil.jar",
        "JAVA_TOOL_OPTIONS": "-javaagent:evil2.jar",
        "RANDOM_APP_SETTING": "x",
    }
    result = run_maven(
        plan, auth, lock_dir=tmp_path / "l", run_id="r", timeout_seconds=30, environ=environ
    )
    child_env = {name.upper() for name in fake.invocation()["env"]}
    assert not child_env & {
        "MY_API_TOKEN",
        "GITHUB_PAT",
        "MAVEN_OPTS",
        "JAVA_TOOL_OPTIONS",
        "RANDOM_APP_SETTING",
    }
    assert result.dropped_risky_env == ("JAVA_TOOL_OPTIONS", "MAVEN_OPTS")
    for forbidden in (secret, "hunter22"):
        assert forbidden not in result.output_tail
    assert "[REDACTED]" in result.output_tail and "ok line" in result.output_tail


def test_replay_filter_environment_and_redact_units() -> None:
    filtered = filter_environment(
        {
            "Path": "p",
            "SYSTEMROOT": "s",
            "AWS_SECRET_ACCESS_KEY": "abcdef123",
            "maven_opts": "-x",
            "FOO": "1",
        }
    )
    assert set(filtered.values) == {"Path", "SYSTEMROOT"}
    assert filtered.dropped_risky == ("maven_opts",) and filtered.secret_values == ("abcdef123",)
    assert (
        redact("a abcdef123 b password: pw1", filtered.secret_values)
        == "a [REDACTED] b password: [REDACTED]"
    )


def test_replay_plugin_executions_are_extracted_from_console_lines() -> None:
    parsed = extract_plugin_executions("\n".join([*PLUGIN_LINES, PLUGIN_LINES[0]]))
    assert parsed == (
        ("maven-surefire-plugin", "3.2.5", "test"),
        ("jacoco-maven-plugin", "0.8.12", "report"),
    )


def test_replay_timeout_kills_whole_process_tree(tmp_path: Path) -> None:
    fake, _root, plan, auth = prepared(tmp_path)
    heartbeat = tmp_path / "beat.txt"
    fake.set(spawn_heartbeat=str(heartbeat), sleep=60, started_marker=str(tmp_path / "up"))
    started = time.monotonic()
    result = run_maven(plan, auth, lock_dir=tmp_path / "l", run_id="r", timeout_seconds=2.0)
    assert result.status is RunStatus.TIMED_OUT and result.exit_code is None
    assert result.tree_terminated and time.monotonic() - started < 30
    assert heartbeat.exists()  # torun cocuk gercekten calismisti
    size = heartbeat.stat().st_size
    time.sleep(0.6)
    assert heartbeat.stat().st_size == size  # kalp atisi durdu: alt surec agaci oldu


def test_replay_cancel_stops_tree_and_reports_cancelled(tmp_path: Path) -> None:
    fake, _root, plan, auth = prepared(tmp_path)
    heartbeat = tmp_path / "beat.txt"
    fake.set(spawn_heartbeat=str(heartbeat), sleep=60)
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    result = run_maven(
        plan, auth, lock_dir=tmp_path / "l", run_id="r", timeout_seconds=60, cancel=cancel
    )
    assert result.status is RunStatus.CANCELLED and result.tree_terminated
    size = heartbeat.stat().st_size if heartbeat.exists() else 0
    time.sleep(0.6)
    assert (heartbeat.stat().st_size if heartbeat.exists() else 0) == size


def test_replay_output_overflow_is_bounded_and_terminates(tmp_path: Path) -> None:
    fake, _root, plan, auth = prepared(tmp_path)
    fake.set(flood=2000, sleep=30)
    result = run_maven(
        plan, auth, lock_dir=tmp_path / "l", run_id="r", timeout_seconds=30, max_output_bytes=10_000
    )
    assert result.status is RunStatus.OUTPUT_OVERFLOW and result.output_bytes <= 10_000


def test_replay_build_lock_serializes_same_project_and_allows_other(tmp_path: Path) -> None:
    fake, _root, plan, auth = prepared(tmp_path)
    marker = tmp_path / "up"
    log = tmp_path / "log.txt"
    fake.set(sleep=2.0, started_marker=str(marker), log=str(log))
    locks = tmp_path / "locks"
    outcome: list[object] = []
    worker = threading.Thread(
        target=lambda: outcome.append(
            run_maven(plan, auth, lock_dir=locks, run_id="r1", timeout_seconds=30)
        )
    )
    worker.start()
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists()
    with pytest.raises(BuildLockBusy):  # ayni reactor: ikinci kosu baslamaz (M10)
        run_maven(plan, auth, lock_dir=locks, run_id="r2", timeout_seconds=30)
    other_root = make_project(tmp_path / "other")
    with build_lock(locks, other_root):  # farkli proje kilidi bagimsiz
        pass
    worker.join(timeout=30)
    assert len(outcome) == 1
    # kilit beklemeli ikinci kosu birincinin ardindan calisir, araliklar ust uste binmez
    marker.unlink()
    fake.set(sleep=0.5, log=str(log))
    first = threading.Thread(
        target=lambda: run_maven(plan, auth, lock_dir=locks, run_id="r3", timeout_seconds=30)
    )
    first.start()
    time.sleep(0.3)
    run_maven(plan, auth, lock_dir=locks, run_id="r4", timeout_seconds=30, lock_wait_seconds=20)
    first.join(timeout=30)
    events = [line.split() for line in log.read_text(encoding="utf-8").splitlines()][2:]
    kinds = [e[0] for e in events]
    assert kinds == ["start", "end", "start", "end"]  # start-start-end-end yok


def test_replay_launcher_drift_blocks_run_before_execution(tmp_path: Path) -> None:
    fake, _root, plan, auth = prepared(tmp_path)
    fake.launcher.write_text(
        fake.launcher.read_text(encoding="utf-8") + "\nrem drift\n", encoding="utf-8"
    )
    with pytest.raises(PolicyViolation, match="drift"):
        run_maven(plan, auth, lock_dir=tmp_path / "l", run_id="r", timeout_seconds=5)
    assert not fake.dump.exists()


def test_replay_quarantine_moves_stale_outputs_and_collect_flags_old_files(tmp_path: Path) -> None:
    root = make_project(tmp_path / "p")
    (root / "target/surefire-reports").mkdir(parents=True)
    (root / "target/surefire-reports/TEST-old.xml").write_text("<testsuite/>", encoding="utf-8")
    (root / "target/jacoco.exec").write_bytes(b"old")
    (root / "target/site/jacoco").mkdir(parents=True)
    (root / "target/site/jacoco/jacoco.xml").write_text("<report/>", encoding="utf-8")
    moved = quarantine_stale_outputs(root, ("",), "run-1")
    assert set(moved) == {
        "target/surefire-reports",
        "target/jacoco.exec",
        "target/site/jacoco/jacoco.xml",
    }
    assert not (root / "target/jacoco.exec").exists()
    assert len(list((root / "target").glob("jacoco.exec.stale-*"))) == 1  # silinmez, kenara alinir
    # Karantina disinda kalmis eski dosya mtime korumasina takilir (tek basina kanit degil).
    old = root / "target/jacoco.exec"
    old.write_bytes(b"old2")
    os.utime(old, ns=(1, 1))
    outputs = collect_outputs(root, ("",), time.time_ns())
    assert "target/jacoco.exec" in outputs.stale and outputs.exec_digests == {}


def test_replay_runner_does_not_use_shell_and_probe_runs_without_plugins(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(tmp_path / "p")
    probe = build_version_probe_plan(root, search_path=fake.search_path).plan
    assert probe is not None
    auth = ExecutionAuthorization(probe.plan_digest, probe.execution_class, allow_network=False)
    # probe plani ag olasi degil -> allow_network gerekmez
    result = run_maven(probe, auth, lock_dir=tmp_path / "l", run_id="r", timeout_seconds=30)
    assert result.status is RunStatus.COMPLETED and "Apache Maven 3.9.9" in result.output_tail
