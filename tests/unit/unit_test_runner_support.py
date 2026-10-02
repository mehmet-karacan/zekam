"""W04 test destegi: fixture proje, FAKE ``mvn`` launcher ve rapor fixture'lari.

Bu makinede gercek Maven yoktur. ``FakeMaven`` tmp dizinde yazilmis kucuk bir Python
betigini ``mvn``/``mvn.cmd`` olarak calistirir; testler bu yuzden ``replay/fake-tool``
kanitidir, gercek Maven kosusu degildir.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

_FAKE_SCRIPT = r"""
import json, os, subprocess, sys, time
from pathlib import Path

scenario = json.loads(Path(SCENARIO).read_text(encoding="utf-8"))
args = sys.argv[1:]
if args == ["-v"]:
    print("Apache Maven 3.9.9 (fake)")
    print("Java version: 21.0.1, vendor: Fake")
    sys.exit(0)
cwd = Path.cwd()
Path(scenario["dump"]).write_text(
    json.dumps({"argv": args, "cwd": str(cwd), "env": sorted(os.environ)}), encoding="utf-8"
)
log = scenario.get("log")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("start %d\n" % time.time_ns())
for rel, content in scenario.get("write", {}).items():
    target = cwd / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
for rel, content in scenario.get("write_bytes", {}).items():
    target = cwd / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes.fromhex(content))
for text in scenario.get("print", []):
    print(text, flush=True)
if scenario.get("spawn_heartbeat"):
    child = "import sys,time\nwhile 1:\n open(sys.argv[1],'a').write('x')\n time.sleep(.05)\n"
    subprocess.Popen([sys.executable, "-c", child, scenario["spawn_heartbeat"]])
if scenario.get("flood"):
    for _ in range(scenario["flood"]):
        sys.stdout.write("x" * 1000 + "\n")
        sys.stdout.flush()
marker = scenario.get("started_marker")
if marker:
    Path(marker).write_text("1", encoding="utf-8")
time.sleep(scenario.get("sleep", 0))
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("end %d\n" % time.time_ns())
sys.exit(scenario.get("exit", 0))
"""

PLUGIN_LINES = [
    "[INFO] --- maven-surefire-plugin:3.2.5:test (default-test) @ app ---",
    "[INFO] --- jacoco-maven-plugin:0.8.12:report (default-cli) @ app ---",
]

JUNIT4_DEP = (
    "<dependency><groupId>junit</groupId><artifactId>junit</artifactId>"
    "<version>4.13.2</version></dependency>"
)
JUPITER_DEP = (
    "<dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>"
    "<version>5.10.2</version></dependency>"
)


def pom(
    *,
    artifact: str = "app",
    props: str = "<maven.compiler.source>1.8</maven.compiler.source>",
    deps: str = JUNIT4_DEP,
    jacoco: bool = True,
    surefire_config: str = "",
    modules: tuple[str, ...] = (),
    packaging: str = "jar",
    extra_plugins: str = "",
) -> str:
    jacoco_xml = (
        "<plugin><groupId>org.jacoco</groupId><artifactId>jacoco-maven-plugin</artifactId>"
        "<version>0.8.12</version><executions><execution><goals><goal>prepare-agent</goal>"
        "</goals></execution></executions></plugin>"
        if jacoco
        else ""
    )
    module_xml = (
        "<modules>" + "".join(f"<module>{m}</module>" for m in modules) + "</modules>"
        if modules
        else ""
    )
    return (
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
        f"<groupId>g</groupId><artifactId>{artifact}</artifactId><version>1</version>"
        f"<packaging>{packaging}</packaging>{module_xml}"
        f"<properties>{props}</properties><dependencies>{deps}</dependencies>"
        f"<build><plugins>{jacoco_xml}"
        "<plugin><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version>"
        f"{surefire_config}</plugin>{extra_plugins}</plugins></build></project>"
    )


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class FakeMaven:
    """tmp dizininde ``mvn`` (Windows'ta ``mvn.cmd``) olarak calisan sahte arac."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.bin = base / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.scenario_path = base / "scenario.json"
        self.dump = base / "dump.json"
        script = base / "fake_mvn.py"
        script.write_text(
            f"SCENARIO = {str(self.scenario_path)!r}\n" + _FAKE_SCRIPT, encoding="utf-8"
        )
        self.script = script
        self.launcher = self.install(self.bin, "mvn")
        self.set()

    def install(self, directory: Path, name: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        python = sys.executable
        if os.name == "nt":
            path = directory / f"{name}.cmd"
            path.write_text(
                f'@echo off\r\n"{python}" "{self.script}" %*\r\nexit /b %ERRORLEVEL%\r\n',
                encoding="utf-8",
            )
        else:
            path = directory / name
            path.write_text(f'#!/bin/sh\nexec "{python}" "{self.script}" "$@"\n', encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def set(self, **scenario: Any) -> None:
        scenario.setdefault("dump", str(self.dump))
        self.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    def invocation(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.dump.read_text(encoding="utf-8"))
        return loaded

    @property
    def search_path(self) -> str:
        return str(self.bin)


# ------------------------------------------------------------------ rapor fixture'lari

DOCTYPE = '<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">'


def counter(kind: str, covered: int, missed: int) -> str:
    return f'<counter type="{kind}" missed="{missed}" covered="{covered}"/>'


def jacoco_xml(*, lines: str, line_counter: str, with_class: bool = True) -> str:
    cls = (
        '<class name="p/A" sourcefilename="A.java">' + counter("INSTRUCTION", 9, 1) + "</class>"
        if with_class
        else ""
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>{DOCTYPE}<report name="app"><package name="p">'
        f'{cls}<sourcefile name="A.java">{lines}{line_counter}</sourcefile></package></report>'
    )


#: LINE 3 covered / 1 missed (satir 10 kismi)
LINES_3_1 = (
    '<line nr="10" mi="1" ci="2" mb="0" cb="0"/><line nr="11" mi="0" ci="3" mb="0" cb="0"/>'
    '<line nr="12" mi="2" ci="0" mb="0" cb="0"/><line nr="13" mi="0" ci="1" mb="0" cb="0"/>'
)
GOOD_JACOCO = jacoco_xml(lines=LINES_3_1, line_counter=counter("LINE", 3, 1))


def surefire_xml(cases: list[tuple[str, str, str]], *, java: str = "21.0.1") -> str:
    body = "".join(
        f'<testcase classname="{c}" name="{n}">{child}</testcase>' for c, n, child in cases
    )
    failures = sum("<failure" in child for _, _, child in cases)
    skipped = sum("<skipped" in child for _, _, child in cases)
    return (
        f'<testsuite name="p.ATest" tests="{len(cases)}" errors="0" skipped="{skipped}" '
        f'failures="{failures}"><properties><property name="java.version" value="{java}"/>'
        f"</properties>{body}</testsuite>"
    )


GOOD_SUREFIRE = surefire_xml([("p.ATest", "a", ""), ("p.ATest", "b", "")])


def make_project(root: Path, *, pom_text: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write(root / "pom.xml", pom_text if pom_text is not None else pom())
    write(root / "src/main/java/p/A.java", "package p; class A { int f(){return 1;} }\n")
    return root


def good_scenario(**overrides: Any) -> dict[str, Any]:
    scenario: dict[str, Any] = {
        "write": {
            "target/surefire-reports/TEST-p.ATest.xml": GOOD_SUREFIRE,
            "target/site/jacoco/jacoco.xml": GOOD_JACOCO,
        },
        "write_bytes": {
            "target/jacoco.exec": "01c0c0",
            "target/classes/p/A.class": "cafebabe01",
            "target/classes/p/A$Inner.class": "cafebabe02",
        },
        "print": list(PLUGIN_LINES),
    }
    scenario.update(overrides)
    return scenario
