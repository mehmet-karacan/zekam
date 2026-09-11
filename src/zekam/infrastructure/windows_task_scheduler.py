"""Read-only Windows Task Scheduler plan and exact registration inspection."""

from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_file_security import windows_user_sid

TASK_NAME = r"\Zekam\AutonomousEvolution"
TICK_INTERVAL_MINUTES = 5
MAX_EXECUTABLE_BYTES = 256 * 1024 * 1024
MAX_TASK_XML_BYTES = 1024 * 1024
TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_SID = re.compile(r"S-1-(?:[0-9]+-)+[0-9]+")


class CommandRunner(Protocol):
    def __call__(self, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]: ...


def _sha256_file(path: Path) -> str:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValidationFailed("Supervisor executable absolute regular file olmali")
    if path.stat().st_size > MAX_EXECUTABLE_BYTES:
        raise PolicyViolation("Supervisor executable fingerprint boyut sinirini asti")
    value = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            value.update(chunk)
    return f"sha256:{value.hexdigest()}"


@dataclass(frozen=True, slots=True)
class WindowsTaskPlan:
    task_name: str
    executable: str
    executable_digest: str
    implementation_manifest: str
    implementation_digest: str
    arguments: tuple[str, ...]
    home: str
    config_digest: str
    principal: str
    principal_sid: str | None
    start_boundary: str
    plan_digest: str

    @classmethod
    def create(
        cls,
        *,
        executable: Path,
        home: Path,
        config_digest: str,
        implementation_manifest: Path,
        start_boundary: dt.datetime,
        principal: str | None = None,
        principal_sid: str | None = None,
    ) -> WindowsTaskPlan:
        parse_digest(config_digest)
        if not home.is_absolute() or home == Path(home.anchor):
            raise ValidationFailed("Supervisor home absolute ve scoped olmali")
        if start_boundary.tzinfo is None:
            raise ValidationFailed("Supervisor start boundary timezone-aware olmali")
        user = (principal or getpass.getuser()).strip()
        if not user or len(user) > 256 or any(ord(char) < 32 for char in user):
            raise ValidationFailed("Supervisor principal bounded olmali")
        resolved_sid = principal_sid
        if resolved_sid is None and principal is None and os.name == "nt":
            resolved_sid = windows_user_sid()
        if resolved_sid is not None and _SID.fullmatch(resolved_sid) is None:
            raise ValidationFailed("Supervisor principal SID gecersiz")
        resolved_executable = executable.resolve(strict=True)
        resolved_home = home.resolve(strict=False)
        executable_digest = _sha256_file(resolved_executable)
        resolved_manifest = implementation_manifest.resolve(strict=True)
        implementation_digest = _sha256_file(resolved_manifest)
        arguments = ("tick", "--home", str(resolved_home))
        boundary = start_boundary.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
        body = {
            "schema": "zekam-windows-supervisor-install-plan/v3",
            "task_name": TASK_NAME,
            "executable": str(resolved_executable),
            "executable_digest": executable_digest,
            "implementation_digest": implementation_digest,
            "implementation_manifest": str(resolved_manifest),
            "arguments": list(arguments),
            "working_directory": str(resolved_home),
            "home": str(resolved_home),
            "config_digest": config_digest,
            "principal": user,
            "principal_sid": resolved_sid,
            "logon_type": "InteractiveToken",
            "run_level": "LeastPrivilege",
            "interval_minutes": TICK_INTERVAL_MINUTES,
            "start_boundary": boundary,
            "start_when_available": True,
            "multiple_instances": "IgnoreNew",
            "disallow_start_if_on_batteries": True,
            "stop_if_going_on_batteries": True,
            "stop_on_idle_end": True,
            "restart_on_idle": False,
            "use_unified_scheduling_engine": True,
            "logoff_supported": False,
            "console_window": False,
            "provider_calls": 0,
            "network_scope": "none",
            "uninstall_target": TASK_NAME,
            "apply": False,
            "authorization_required": True,
            "grants_authority": False,
        }
        return cls(
            TASK_NAME,
            str(resolved_executable),
            executable_digest,
            str(resolved_manifest),
            implementation_digest,
            arguments,
            str(resolved_home),
            config_digest,
            user,
            resolved_sid,
            boundary,
            digest(body),
        )

    def as_dict(self) -> dict[str, Any]:
        body = {
            "schema": "zekam-windows-supervisor-install-plan/v3",
            "task_name": self.task_name,
            "executable": self.executable,
            "executable_digest": self.executable_digest,
            "implementation_digest": self.implementation_digest,
            "implementation_manifest": self.implementation_manifest,
            "arguments": list(self.arguments),
            "working_directory": self.home,
            "home": self.home,
            "config_digest": self.config_digest,
            "principal": self.principal,
            "principal_sid": self.principal_sid,
            "logon_type": "InteractiveToken",
            "run_level": "LeastPrivilege",
            "interval_minutes": TICK_INTERVAL_MINUTES,
            "start_boundary": self.start_boundary,
            "start_when_available": True,
            "multiple_instances": "IgnoreNew",
            "disallow_start_if_on_batteries": True,
            "stop_if_going_on_batteries": True,
            "stop_on_idle_end": True,
            "restart_on_idle": False,
            "use_unified_scheduling_engine": True,
            "logoff_supported": False,
            "console_window": False,
            "provider_calls": 0,
            "network_scope": "none",
            "uninstall_target": self.task_name,
            "apply": False,
            "authorization_required": True,
            "grants_authority": False,
        }
        if digest(body) != self.plan_digest:
            raise PolicyViolation("Windows supervisor plan digest drift")
        return body | {"plan_digest": self.plan_digest}


def inspect_windows_task(
    plan: WindowsTaskPlan,
    *,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Read the exact named task without creating, updating or enabling it."""

    execute = runner or _run
    result = execute(_query_arguments(plan))
    if result.returncode == 3:
        present = False
    elif result.returncode == 0:
        present = True
    else:
        raise PolicyViolation("Windows supervisor Task Scheduler query basarisiz")
    xml = result.stdout if present else ""
    matches = present and _xml_matches_plan(xml, plan)
    executable_current = _sha256_file(Path(plan.executable)) == plan.executable_digest
    implementation_current = (
        _sha256_file(Path(plan.implementation_manifest)) == plan.implementation_digest
    )
    matches = matches and executable_current and implementation_current
    body = {
        "schema": "zekam-windows-supervisor-status/v1",
        "task_name": plan.task_name,
        "state": "matching" if matches else ("drifted" if present else "absent"),
        "registered": present,
        "matches_plan": matches,
        "executable_current": executable_current,
        "implementation_current": implementation_current,
        "plan_digest": plan.plan_digest,
        "executable_digest": plan.executable_digest,
        "implementation_digest": plan.implementation_digest,
        "logoff_supported": False,
        "read_only": True,
        "grants_authority": False,
    }
    return body | {"status_digest": digest(body)}


def _xml_matches_plan(xml: str, plan: WindowsTaskPlan) -> bool:
    if len(xml.encode("utf-8", errors="replace")) > MAX_TASK_XML_BYTES:
        return False
    try:
        actual = ET.fromstring(xml)
    except ET.ParseError:
        return False
    return _semantic_task_matches(actual, plan)


def _semantic_task_matches(task: ET.Element, plan: WindowsTaskPlan) -> bool:
    """Match Windows' native normalization inside a closed task envelope."""

    root = _children(
        task,
        {"RegistrationInfo", "Triggers", "Principals", "Settings", "Actions"},
        attributes={"version": "1.4"},
    )
    if task.tag != _tag("Task") or task.attrib != {"version": "1.4"} or root is None:
        return False

    registration = _children(root["RegistrationInfo"], {"URI", "Description"})
    if registration is None or not _leaf(registration["URI"], plan.task_name):
        return False
    if not _leaf(registration["Description"], _registration_marker(plan)):
        return False

    principals = _children(root["Principals"], {"Principal"})
    if principals is None:
        return False
    principal = principals["Principal"]
    principal_fields = _children(
        principal, {"UserId", "LogonType"}, optional={"RunLevel"}, attributes={"id": "Author"}
    )
    if principal_fields is None:
        return False
    user_id = _text(principal_fields["UserId"])
    if user_id not in {plan.principal, plan.principal_sid}:
        return False
    if not _leaf(principal_fields["LogonType"], "InteractiveToken"):
        return False
    if "RunLevel" in principal_fields and not _leaf(
        principal_fields["RunLevel"], "LeastPrivilege"
    ):
        return False

    triggers = _children(root["Triggers"], {"TimeTrigger"})
    if triggers is None:
        return False
    trigger = _children(
        triggers["TimeTrigger"],
        {"Repetition", "StartBoundary"},
        optional={"Enabled"},
    )
    if trigger is None or not _same_instant(
        _text(trigger["StartBoundary"]), plan.start_boundary
    ):
        return False
    if "Enabled" in trigger and not _leaf(trigger["Enabled"], "true"):
        return False
    repetition = _children(
        trigger["Repetition"], {"Interval"}, optional={"StopAtDurationEnd"}
    )
    if repetition is None or not _leaf(
        repetition["Interval"], f"PT{TICK_INTERVAL_MINUTES}M"
    ):
        return False
    if "StopAtDurationEnd" in repetition and not _leaf(
        repetition["StopAtDurationEnd"], "false"
    ):
        return False

    settings = _children(
        root["Settings"],
        {
            "MultipleInstancesPolicy",
            "DisallowStartIfOnBatteries",
            "StopIfGoingOnBatteries",
            "IdleSettings",
            "StartWhenAvailable",
            "ExecutionTimeLimit",
            "UseUnifiedSchedulingEngine",
        },
        optional={"Enabled"},
    )
    if settings is None:
        return False
    expected_settings = {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "true",
        "StopIfGoingOnBatteries": "true",
        "StartWhenAvailable": "true",
        "ExecutionTimeLimit": "PT5M",
        "UseUnifiedSchedulingEngine": "true",
    }
    if any(not _leaf(settings[name], value) for name, value in expected_settings.items()):
        return False
    if "Enabled" in settings and not _leaf(settings["Enabled"], "true"):
        return False
    idle = _children(settings["IdleSettings"], {"StopOnIdleEnd", "RestartOnIdle"})
    if idle is None or not _leaf(idle["StopOnIdleEnd"], "true"):
        return False
    if not _leaf(idle["RestartOnIdle"], "false"):
        return False

    actions = _children(root["Actions"], {"Exec"}, attributes={"Context": "Author"})
    if actions is None:
        return False
    action = _children(actions["Exec"], {"Command", "Arguments", "WorkingDirectory"})
    if action is None:
        return False
    return (
        _leaf(action["Command"], plan.executable)
        and _leaf(action["Arguments"], subprocess.list2cmdline(plan.arguments))
        and _leaf(action["WorkingDirectory"], plan.home)
    )


def _children(
    element: ET.Element,
    required: set[str],
    *,
    optional: set[str] | None = None,
    attributes: dict[str, str] | None = None,
) -> dict[str, ET.Element] | None:
    """Return unique qualified children while rejecting every unknown capability."""

    optional = optional or set()
    if element.attrib != (attributes or {}) or (element.text or "").strip():
        return None
    allowed = required | optional
    result: dict[str, ET.Element] = {}
    for child in element:
        if (child.tail or "").strip():
            return None
        prefix = f"{{{TASK_XML_NAMESPACE}}}"
        if not child.tag.startswith(prefix):
            return None
        name = child.tag.removeprefix(prefix)
        if name not in allowed or name in result:
            return None
        result[name] = child
    if not required.issubset(result):
        return None
    return result


def _leaf(element: ET.Element, expected: str) -> bool:
    return (
        element.attrib == {}
        and len(element) == 0
        and (element.text or "") == expected
    )


def _text(element: ET.Element) -> str:
    return element.text or ""


def _same_instant(actual: str, expected: str) -> bool:
    try:
        actual_time = dt.datetime.fromisoformat(actual.replace("Z", "+00:00"))
        expected_time = dt.datetime.fromisoformat(expected.replace("Z", "+00:00"))
    except ValueError:
        return False
    if actual_time.tzinfo is None or expected_time.tzinfo is None:
        return False
    return actual_time.astimezone(dt.UTC) == expected_time.astimezone(dt.UTC)


def _tag(name: str) -> str:
    return f"{{{TASK_XML_NAMESPACE}}}{name}"


def install_windows_task(
    plan: WindowsTaskPlan,
    *,
    authorized_plan_digest: str,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Install exactly the reviewed task and require an exact readback."""

    parse_digest(authorized_plan_digest)
    if authorized_plan_digest != plan.plan_digest:
        raise PolicyViolation("Windows supervisor install exact plan authorization ister")
    if _sha256_file(Path(plan.executable)) != plan.executable_digest:
        raise PolicyViolation("Windows supervisor executable install oncesi drift")
    if _sha256_file(Path(plan.implementation_manifest)) != plan.implementation_digest:
        raise PolicyViolation("Windows supervisor implementation install oncesi drift")
    execute = runner or _run
    before = inspect_windows_task(plan, runner=execute)
    if before["state"] == "drifted":
        raise PolicyViolation("Windows supervisor ayni adli drifted task overwrite edilemez")
    if before["state"] == "matching":
        body = {
            "schema": "zekam-windows-supervisor-install-receipt/v1",
            "task_name": plan.task_name,
            "plan_digest": plan.plan_digest,
            "status_digest": before["status_digest"],
            "state": "already-installed",
            "idempotent": True,
            "grants_authority": False,
        }
        return body | {"receipt_digest": digest(body)}
    payload = _task_xml(plan)
    descriptor, name = tempfile.mkstemp(prefix="zekam-task-", suffix=".xml")
    try:
        import os

        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        result = execute(
            ("schtasks.exe", "/Create", "/TN", plan.task_name, "/XML", name)
        )
        if result.returncode != 0:
            raise PolicyViolation("Windows supervisor Task Scheduler kaydi basarisiz")
        try:
            status = inspect_windows_task(plan, runner=execute)
        except Exception as exc:
            _rollback_created_task(plan, execute)
            raise PolicyViolation(
                "Windows supervisor kurulum readback basarisiz; kayit geri alindi"
            ) from exc
        if status["state"] != "matching":
            _rollback_created_task(plan, execute)
            raise PolicyViolation("Windows supervisor kurulum readback drift")
        body = {
            "schema": "zekam-windows-supervisor-install-receipt/v1",
            "task_name": plan.task_name,
            "plan_digest": plan.plan_digest,
            "status_digest": status["status_digest"],
            "state": "installed",
            "idempotent": True,
            "grants_authority": False,
        }
        return body | {"receipt_digest": digest(body)}
    finally:
        Path(name).unlink(missing_ok=True)


def _rollback_created_task(plan: WindowsTaskPlan, execute: CommandRunner) -> None:
    """Remove only the task just created and prove the exact name is absent."""

    rollback = execute(("schtasks.exe", "/Delete", "/TN", plan.task_name, "/F"))
    if rollback.returncode != 0:
        raise PolicyViolation("Windows supervisor kurulum recovery-required")
    try:
        after = inspect_windows_task(plan, runner=execute)
    except Exception as exc:
        raise PolicyViolation("Windows supervisor kurulum recovery-required") from exc
    if after["state"] != "absent":
        raise PolicyViolation("Windows supervisor kurulum recovery-required")


def uninstall_windows_task(
    plan: WindowsTaskPlan,
    *,
    authorized_plan_digest: str,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Delete only the exact Zekam task after digest-bound authorization."""

    parse_digest(authorized_plan_digest)
    if authorized_plan_digest != plan.plan_digest:
        raise PolicyViolation("Windows supervisor uninstall exact plan authorization ister")
    execute = runner or _run
    before = inspect_windows_task(plan, runner=execute)
    if before["state"] == "absent":
        state = "already-absent"
    elif before["state"] != "matching":
        raise PolicyViolation("Windows supervisor drifted/yabanci task silinemez")
    else:
        result = execute(("schtasks.exe", "/Delete", "/TN", plan.task_name, "/F"))
        if result.returncode != 0:
            raise PolicyViolation("Windows supervisor exact task silme basarisiz")
        after = inspect_windows_task(plan, runner=execute)
        if after["state"] != "absent":
            raise PolicyViolation("Windows supervisor uninstall readback drift")
        state = "uninstalled"
    body = {
        "schema": "zekam-windows-supervisor-uninstall-receipt/v1",
        "task_name": plan.task_name,
        "plan_digest": plan.plan_digest,
        "state": state,
        "grants_authority": False,
    }
    return body | {"receipt_digest": digest(body)}


def _task_xml(plan: WindowsTaskPlan) -> bytes:
    namespace = TASK_XML_NAMESPACE
    ET.register_namespace("", namespace)

    def tag(name: str) -> str:
        return f"{{{namespace}}}{name}"

    task = ET.Element(tag("Task"), {"version": "1.4"})
    registration = ET.SubElement(task, tag("RegistrationInfo"))
    ET.SubElement(registration, tag("URI")).text = plan.task_name
    ET.SubElement(registration, tag("Description")).text = _registration_marker(plan)
    triggers = ET.SubElement(task, tag("Triggers"))
    trigger = ET.SubElement(triggers, tag("TimeTrigger"))
    repetition = ET.SubElement(trigger, tag("Repetition"))
    ET.SubElement(repetition, tag("Interval")).text = f"PT{TICK_INTERVAL_MINUTES}M"
    ET.SubElement(repetition, tag("StopAtDurationEnd")).text = "false"
    ET.SubElement(trigger, tag("StartBoundary")).text = plan.start_boundary
    ET.SubElement(trigger, tag("Enabled")).text = "true"
    principals = ET.SubElement(task, tag("Principals"))
    principal = ET.SubElement(principals, tag("Principal"), {"id": "Author"})
    ET.SubElement(principal, tag("UserId")).text = plan.principal
    ET.SubElement(principal, tag("LogonType")).text = "InteractiveToken"
    ET.SubElement(principal, tag("RunLevel")).text = "LeastPrivilege"
    settings = ET.SubElement(task, tag("Settings"))
    ET.SubElement(settings, tag("MultipleInstancesPolicy")).text = "IgnoreNew"
    ET.SubElement(settings, tag("DisallowStartIfOnBatteries")).text = "true"
    ET.SubElement(settings, tag("StopIfGoingOnBatteries")).text = "true"
    idle = ET.SubElement(settings, tag("IdleSettings"))
    ET.SubElement(idle, tag("StopOnIdleEnd")).text = "true"
    ET.SubElement(idle, tag("RestartOnIdle")).text = "false"
    ET.SubElement(settings, tag("StartWhenAvailable")).text = "true"
    ET.SubElement(settings, tag("Enabled")).text = "true"
    ET.SubElement(settings, tag("ExecutionTimeLimit")).text = "PT5M"
    ET.SubElement(settings, tag("UseUnifiedSchedulingEngine")).text = "true"
    actions = ET.SubElement(task, tag("Actions"), {"Context": "Author"})
    action = ET.SubElement(actions, tag("Exec"))
    ET.SubElement(action, tag("Command")).text = plan.executable
    ET.SubElement(action, tag("Arguments")).text = subprocess.list2cmdline(plan.arguments)
    ET.SubElement(action, tag("WorkingDirectory")).text = plan.home
    encoded = ET.tostring(task, encoding="utf-16", xml_declaration=True)
    if not isinstance(encoded, bytes):
        raise PolicyViolation("Windows supervisor task XML encoding drift")
    return encoded


def _registration_marker(plan: WindowsTaskPlan) -> str:
    return (
        "ZEKAM_SUPERVISOR_V3|"
        f"plan={plan.plan_digest}|executable={plan.executable_digest}|"
        f"implementation={plan.implementation_digest}|config={plan.config_digest}"
    )


def _query_arguments(plan: WindowsTaskPlan) -> tuple[str, ...]:
    script = (
        "$ErrorActionPreference='Stop';"
        f"try {{ Export-ScheduledTask -TaskName '{plan.task_name.rsplit(chr(92), 1)[-1]}' "
        "-TaskPath '\\Zekam\\' } "
        "catch { if ($_.FullyQualifiedErrorId -like 'HRESULT 0x80070002,*') "
        "{ exit 3 }; exit 4 }"
    )
    return (
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "RemoteSigned",
        "-Command",
        script,
    )


def _run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
