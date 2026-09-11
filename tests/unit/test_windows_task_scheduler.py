from __future__ import annotations

import datetime as dt
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.windows_task_scheduler import (
    TASK_NAME,
    WindowsTaskPlan,
    _task_xml,
    inspect_windows_task,
    install_windows_task,
    uninstall_windows_task,
)

NOW = dt.datetime(2026, 9, 6, 12, 5, tzinfo=dt.UTC)


def _plan(tmp_path: Path, *, principal_sid: str | None = None) -> WindowsTaskPlan:
    executable = tmp_path / "zekam-background.exe"
    executable.write_bytes(b"reviewed-zekam-executable")
    manifest = tmp_path / "PACKAGE_RELEASE_MANIFEST.json"
    manifest.write_bytes(b"reviewed-package-manifest")
    return WindowsTaskPlan.create(
        executable=executable,
        home=tmp_path / "home",
        config_digest=digest("config"),
        implementation_manifest=manifest,
        start_boundary=NOW,
        principal="DOMAIN\\user",
        principal_sid=principal_sid,
    )


def test_windows_supervisor_plan_is_exact_read_only_and_requires_authorization(
    tmp_path: Path,
) -> None:
    document = _plan(tmp_path).as_dict()
    assert document["task_name"] == TASK_NAME
    assert document["schema"] == "zekam-windows-supervisor-install-plan/v3"
    assert document["executable"].endswith("zekam-background.exe")
    assert document["arguments"] == ["tick", "--home", str(tmp_path / "home")]
    assert document["console_window"] is False
    assert document["interval_minutes"] == 5
    assert document["multiple_instances"] == "IgnoreNew"
    assert document["principal_sid"] is None
    assert document["disallow_start_if_on_batteries"] is True
    assert document["stop_if_going_on_batteries"] is True
    assert document["use_unified_scheduling_engine"] is True
    assert document["start_when_available"] is True
    assert document["logon_type"] == "InteractiveToken"
    assert document["logoff_supported"] is False
    assert document["apply"] is False
    assert document["authorization_required"] is True
    assert document["network_scope"] == "none"


def test_windows_supervisor_status_distinguishes_absent_matching_and_drift(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)

    def absent(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        assert arguments[0] == "powershell.exe"
        return subprocess.CompletedProcess(arguments, 3, "", "not found")

    assert inspect_windows_task(plan, runner=absent)["state"] == "absent"
    def denied(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 4, "", "access denied")

    with pytest.raises(PolicyViolation, match="query"):
        inspect_windows_task(plan, runner=denied)
    xml = _task_xml(plan).decode("utf-16")
    def matching(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, xml, "")

    assert inspect_windows_task(plan, runner=matching)["state"] == "matching"

    def drifted(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, xml.replace("IgnoreNew", "Parallel"), "")

    assert inspect_windows_task(plan, runner=drifted)["state"] == "drifted"


def test_windows_supervisor_accepts_only_native_semantic_normalization(
    tmp_path: Path,
) -> None:
    sid = "S-1-5-21-100-200-300-400"
    plan = _plan(tmp_path, principal_sid=sid)
    root = ET.fromstring(_task_xml(plan))
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"

    def tag(name: str) -> str:
        return f"{{{namespace}}}{name}"

    principal = root.find(f"{tag('Principals')}/{tag('Principal')}")
    assert principal is not None
    user_id = principal.find(tag("UserId"))
    run_level = principal.find(tag("RunLevel"))
    assert user_id is not None and run_level is not None
    user_id.text = sid
    principal.remove(run_level)

    trigger = root.find(f"{tag('Triggers')}/{tag('TimeTrigger')}")
    assert trigger is not None
    boundary = trigger.find(tag("StartBoundary"))
    enabled = trigger.find(tag("Enabled"))
    repetition = trigger.find(tag("Repetition"))
    assert boundary is not None and enabled is not None and repetition is not None
    boundary.text = "2026-09-06T15:05:00+03:00"
    trigger.remove(enabled)
    stop_at_end = repetition.find(tag("StopAtDurationEnd"))
    assert stop_at_end is not None
    repetition.remove(stop_at_end)

    settings = root.find(tag("Settings"))
    assert settings is not None
    settings_enabled = settings.find(tag("Enabled"))
    assert settings_enabled is not None
    settings.remove(settings_enabled)

    root[:] = list(reversed(list(root)))
    for child in root:
        child[:] = list(reversed(list(child)))
    xml = ET.tostring(root, encoding="unicode")

    def runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, xml, "")

    assert inspect_windows_task(plan, runner=runner)["state"] == "matching"


def test_windows_supervisor_principal_sid_is_exact_and_validated(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    xml = _task_xml(plan).decode("utf-16").replace(
        "DOMAIN\\user", "S-1-5-21-100-200-300-999"
    )

    def runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, xml, "")

    assert inspect_windows_task(plan, runner=runner)["state"] == "drifted"
    with pytest.raises(ValidationFailed, match="SID"):
        _plan(tmp_path, principal_sid="not-a-sid")


@pytest.mark.parametrize(
    ("xpath", "value"),
    [
        ("Settings/DisallowStartIfOnBatteries", "false"),
        ("Settings/IdleSettings/RestartOnIdle", "true"),
        ("Triggers/TimeTrigger/StartBoundary", "2026-09-06T12:06:00Z"),
        ("Triggers/TimeTrigger/Enabled", "false"),
        ("Triggers/TimeTrigger/Repetition/StopAtDurationEnd", "true"),
        ("Settings/Enabled", "false"),
        ("Principals/Principal/RunLevel", "HighestAvailable"),
    ],
)
def test_windows_supervisor_rejects_native_semantic_drift(
    tmp_path: Path, xpath: str, value: str
) -> None:
    plan = _plan(tmp_path)
    root = ET.fromstring(_task_xml(plan))
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    qualified = "/".join(f"{{{namespace}}}{part}" for part in xpath.split("/"))
    element = root.find(qualified)
    assert element is not None
    element.text = value
    xml = ET.tostring(root, encoding="unicode")

    def runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, xml, "")

    assert inspect_windows_task(plan, runner=runner)["state"] == "drifted"


@pytest.mark.parametrize(
    "xpath",
    [
        "Settings",
        "Settings/IdleSettings",
        "Principals",
        "Actions/Exec",
        "Triggers/TimeTrigger",
    ],
)
def test_windows_supervisor_rejects_nested_extra_capabilities(
    tmp_path: Path, xpath: str
) -> None:
    plan = _plan(tmp_path)
    root = ET.fromstring(_task_xml(plan))
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    qualified = "/".join(f"{{{namespace}}}{part}" for part in xpath.split("/"))
    parent = root.find(qualified)
    assert parent is not None
    ET.SubElement(parent, f"{{{namespace}}}WakeToRun").text = "true"
    xml = ET.tostring(root, encoding="unicode")

    def runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, xml, "")

    assert inspect_windows_task(plan, runner=runner)["state"] == "drifted"


def test_windows_supervisor_rejects_sensitive_leaf_whitespace_and_large_xml(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    xml = _task_xml(plan).decode("utf-16")

    def whitespace(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        changed = xml.replace("</Arguments>", " </Arguments>")
        return subprocess.CompletedProcess(arguments, 0, changed, "")

    def oversized(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, xml + (" " * 1_048_577), "")

    assert inspect_windows_task(plan, runner=whitespace)["state"] == "drifted"
    assert inspect_windows_task(plan, runner=oversized)["state"] == "drifted"

    root = ET.fromstring(_task_xml(plan))
    first = next(iter(root))
    first.tail = "unauthorized-mixed-content"
    mixed = ET.tostring(root, encoding="unicode")

    def mixed_content(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, mixed, "")

    assert inspect_windows_task(plan, runner=mixed_content)["state"] == "drifted"


@pytest.mark.parametrize("extra_kind", ["action", "trigger", "attribute", "element"])
def test_windows_supervisor_rejects_every_extra_xml_capability(
    tmp_path: Path, extra_kind: str
) -> None:
    plan = _plan(tmp_path)
    root = ET.fromstring(_task_xml(plan))
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"

    def tag(name: str) -> str:
        return f"{{{namespace}}}{name}"

    if extra_kind == "action":
        actions = root.find(tag("Actions"))
        assert actions is not None
        action = ET.SubElement(actions, tag("Exec"))
        ET.SubElement(action, tag("Command")).text = r"C:\malicious.exe"
    elif extra_kind == "trigger":
        triggers = root.find(tag("Triggers"))
        assert triggers is not None
        ET.SubElement(triggers, tag("TimeTrigger"))
    elif extra_kind == "attribute":
        root.set("foreign", "true")
    else:
        ET.SubElement(root, tag("ComHandler"))
    xml = ET.tostring(root, encoding="unicode")

    def runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, xml, "")

    assert inspect_windows_task(plan, runner=runner)["state"] == "drifted"


def test_install_and_uninstall_require_exact_digest_and_terminal_readback(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    registered: list[str] = []

    def runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        if arguments[1] == "/Create":
            xml_path = Path(arguments[arguments.index("/XML") + 1])
            registered[:] = [xml_path.read_text(encoding="utf-16")]
            return subprocess.CompletedProcess(arguments, 0, "created", "")
        if arguments[1] == "/Delete":
            registered.clear()
            return subprocess.CompletedProcess(arguments, 0, "deleted", "")
        return subprocess.CompletedProcess(
            arguments,
            0 if registered else 3,
            registered[0] if registered else "",
            "",
        )

    with pytest.raises(PolicyViolation, match="exact plan"):
        install_windows_task(
            plan,
            authorized_plan_digest=digest("wrong"),
            runner=runner,
        )
    installed = install_windows_task(
        plan,
        authorized_plan_digest=plan.plan_digest,
        runner=runner,
    )
    assert installed["state"] == "installed"
    assert install_windows_task(
        plan,
        authorized_plan_digest=plan.plan_digest,
        runner=runner,
    )["state"] == "already-installed"
    removed = uninstall_windows_task(
        plan,
        authorized_plan_digest=plan.plan_digest,
        runner=runner,
    )
    assert removed["state"] == "uninstalled"


def test_drifted_same_name_is_never_overwritten_or_deleted(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    drifted_xml = _task_xml(plan).decode("utf-16").replace(
        "ZEKAM_SUPERVISOR_V3", "FOREIGN_TASK"
    )

    def runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 0, drifted_xml, "")

    with pytest.raises(PolicyViolation, match="overwrite"):
        install_windows_task(
            plan, authorized_plan_digest=plan.plan_digest, runner=runner
        )
    with pytest.raises(PolicyViolation, match="silinemez"):
        uninstall_windows_task(
            plan, authorized_plan_digest=plan.plan_digest, runner=runner
        )


def test_install_rolls_back_and_proves_absent_when_readback_query_fails(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    registered = False
    query_count = 0
    calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        nonlocal registered, query_count
        calls.append(arguments)
        if arguments[0] == "schtasks.exe" and arguments[1] == "/Create":
            registered = True
            return subprocess.CompletedProcess(arguments, 0, "created", "")
        if arguments[0] == "schtasks.exe" and arguments[1] == "/Delete":
            registered = False
            return subprocess.CompletedProcess(arguments, 0, "deleted", "")
        query_count += 1
        if query_count == 1:
            return subprocess.CompletedProcess(arguments, 3, "", "not found")
        if query_count == 2:
            return subprocess.CompletedProcess(arguments, 4, "", "access denied")
        return subprocess.CompletedProcess(arguments, 0 if registered else 3, "", "")

    with pytest.raises(PolicyViolation, match="geri alindi"):
        install_windows_task(
            plan, authorized_plan_digest=plan.plan_digest, runner=runner
        )
    assert registered is False
    assert any(call[0] == "schtasks.exe" and call[1] == "/Delete" for call in calls)


def test_install_reports_recovery_required_if_rollback_absence_is_unprovable(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    query_count = 0

    def runner(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        nonlocal query_count
        if arguments[0] == "schtasks.exe":
            return subprocess.CompletedProcess(arguments, 0, "ok", "")
        query_count += 1
        if query_count == 1:
            return subprocess.CompletedProcess(arguments, 3, "", "not found")
        return subprocess.CompletedProcess(arguments, 4, "", "query failed")

    with pytest.raises(PolicyViolation, match="recovery-required"):
        install_windows_task(
            plan, authorized_plan_digest=plan.plan_digest, runner=runner
        )
