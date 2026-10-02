"""W04: POM statik kesfi, Maven plani, arac eksigi, yetki ve yol guvenligi (fake-tool).

Gercek Maven yoktur; launcher tmp dizinindeki FakeMaven'dir (replay/fake-tool).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from tests.unit.unit_test_runner_support import (
    JUNIT4_DEP,
    JUPITER_DEP,
    FakeMaven,
    make_project,
    pom,
    write,
)

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.unit_test_runner.maven_plan import (
    ExecutionAuthorization,
    ExecutionClass,
    LauncherKind,
    MavenPlan,
    PlanStatus,
    build_effective_pom_plan,
    build_unit_test_plan,
    build_version_probe_plan,
    check_authorization,
    parse_version_output,
    verify_launcher,
)
from zekam.infrastructure.unit_test_runner.path_safety import resolve_inside
from zekam.infrastructure.unit_test_runner.pom_inspect import (
    JUnitEngine,
    Severity,
    discover_project,
)


def codes(root: Path) -> set[str]:
    return {f.code for f in discover_project(root).findings}


# ------------------------------------------------------------------ discovery / preflight


def test_replay_discovery_engines_release_and_multimodule(tmp_path: Path) -> None:
    root = make_project(
        tmp_path / "multi",
        pom_text=pom(
            artifact="parent",
            packaging="pom",
            modules=("legacy", "modern"),
            deps="",
        ),
    )
    write(
        root / "legacy/pom.xml",
        pom(artifact="legacy", props="<maven.compiler.source>1.8</maven.compiler.source>"),
    )
    write(
        root / "modern/pom.xml",
        pom(
            artifact="modern",
            props="<maven.compiler.release>17</maven.compiler.release>",
            deps=JUPITER_DEP,
        ),
    )
    discovery = discover_project(root)
    assert discovery.module_dirs == ("", "legacy", "modern")
    legacy, modern = discovery.module("legacy"), discovery.module("modern")
    assert legacy is not None and modern is not None
    assert (legacy.engine, legacy.compile_release) == (JUnitEngine.JUNIT4, "1.8")
    assert (modern.engine, modern.compile_release) == (JUnitEngine.JUNIT_PLATFORM, "17")
    assert modern.has_prepare_agent and modern.jacoco_version == "0.8.12"
    assert discovery.static_only  # profil/parent cozumleme yok; acikca isaretli


def test_replay_discovery_engine_unknown_mixed_and_inherited_deps(tmp_path: Path) -> None:
    root = make_project(
        tmp_path / "p",
        pom_text=pom(
            artifact="parent", packaging="pom", modules=("child",), deps=JUNIT4_DEP + JUPITER_DEP
        ),
    )
    write(root / "child/pom.xml", pom(artifact="child", deps="", jacoco=False))
    discovery = discover_project(root)
    child = discovery.module("child")
    assert child is not None
    assert child.engine is JUnitEngine.MIXED  # parent dependencies miras
    assert child.has_prepare_agent  # parent plugin'i miras
    bare = make_project(tmp_path / "bare", pom_text=pom(deps=""))
    assert discover_project(bare).modules[0].engine is JUnitEngine.UNKNOWN
    assert "engine-unknown" in codes(bare)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"props": "<skipTests>true</skipTests>"}, "tests-skipped"),
        ({"props": "<maven.test.skip>true</maven.test.skip>"}, "tests-skipped"),
        ({"props": "<jacoco.skip>true</jacoco.skip>"}, "jacoco-skip"),
        (
            {"surefire_config": "<configuration><forkCount>0</forkCount></configuration>"},
            "fork-count-zero",
        ),
        (
            {"surefire_config": "<configuration><skipTests>true</skipTests></configuration>"},
            "tests-skipped",
        ),
        (
            {"surefire_config": "<configuration><argLine>-Xmx1g</argLine></configuration>"},
            "argline-agent-override",
        ),
        (
            {
                "surefire_config": (
                    "<configuration><testFailureIgnore>true</testFailureIgnore></configuration>"
                )
            },
            "test-failure-ignore",
        ),
        (
            {
                "surefire_config": (
                    "<configuration><includes><include>**/*IT.java</include>"
                    "</includes></configuration>"
                )
            },
            "integration-in-unit-surefire",
        ),
    ],
)
def test_replay_preflight_blockers_are_explicit(
    tmp_path: Path, kwargs: dict[str, Any], code: str
) -> None:
    root = make_project(tmp_path / "p", pom_text=pom(**kwargs))
    findings = [f for f in discover_project(root).findings if f.code == code]
    assert findings and findings[0].severity is Severity.BLOCKER
    result = build_unit_test_plan(root, target_modules=("",), search_path=str(tmp_path / "nobin"))
    assert result.status is PlanStatus.SETUP_REQUIRED and result.plan is None
    assert any(code in reason for reason in result.reasons)


def test_replay_argline_that_preserves_agent_property_is_not_a_blocker(tmp_path: Path) -> None:
    config = "<configuration><argLine>@{argLine} -Xmx1g</argLine></configuration>"
    root = make_project(tmp_path / "p", pom_text=pom(surefire_config=config))
    assert "argline-agent-override" not in codes(root)


def test_replay_discovery_flags_failsafe_exec_plugins_and_extensions(tmp_path: Path) -> None:
    extra = (
        "<plugin><artifactId>maven-failsafe-plugin</artifactId></plugin>"
        "<plugin><artifactId>exec-maven-plugin</artifactId></plugin>"
    )
    root = make_project(tmp_path / "p", pom_text=pom(extra_plugins=extra))
    write(root / ".mvn/extensions.xml", "<extensions/>")
    found = codes(root)
    assert {"failsafe-present", "custom-exec-plugin", "maven-extensions"} <= found


def test_replay_discovery_config_digest_changes_with_pom_and_mvn_config(tmp_path: Path) -> None:
    root = make_project(tmp_path / "p")
    first = discover_project(root).config_digest
    write(root / ".mvn/jvm.config", "-Xmx2g")
    second = discover_project(root).config_digest
    write(root / "pom.xml", pom(props="<maven.compiler.source>11</maven.compiler.source>"))
    assert len({first, second, discover_project(root).config_digest}) == 3


# ------------------------------------------------------------------ plan / tool missing / setup


def test_replay_plan_is_explicit_argv_with_goals_and_plugin_record(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(tmp_path / "p")
    result = build_unit_test_plan(
        root, target_modules=("",), test_selection=("p.ATest",), search_path=fake.search_path
    )
    assert result.status is PlanStatus.READY and result.plan is not None
    plan = result.plan
    assert plan.goals == ("test", "jacoco:report")  # tek basina `test` yeterli sayilmaz
    assert plan.args == (
        "-B",
        "-Dtest=p.ATest",
        "-Dsurefire.failIfNoSpecifiedTests=false",
        "test",
        "jacoco:report",
    )
    assert plan.launcher.kind is LauncherKind.INSTALLED
    assert dict(plan.declared_plugins) == {
        "jacoco-maven-plugin": "0.8.12",
        "maven-surefire-plugin": "3.2.5",
    }
    assert plan.network_isolation == "not-provided"  # offline/izolasyon iddiasi yok
    assert plan.execution_class is ExecutionClass.PLUGIN_EXECUTION
    assert plan.plan_digest.startswith("sha256:")


def test_replay_plan_multimodule_uses_pl_for_non_root_targets(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(
        tmp_path / "m", pom_text=pom(artifact="parent", packaging="pom", modules=("svc",))
    )
    write(root / "svc/pom.xml", pom(artifact="svc"))
    result = build_unit_test_plan(root, target_modules=("svc",), search_path=fake.search_path)
    assert result.plan is not None
    assert result.plan.args[:4] == ("-B", "-pl", "svc", "-am")


def test_replay_tool_missing_returns_setup_plan_without_measurement(tmp_path: Path) -> None:
    root = make_project(tmp_path / "p")
    empty = tmp_path / "empty"
    empty.mkdir()
    result = build_unit_test_plan(root, target_modules=("",), search_path=str(empty))
    assert result.status is PlanStatus.TOOL_MISSING and result.plan is None
    assert result.setup_plan and "kurulmaz" in result.setup_plan[-1]


def test_replay_setup_required_when_jacoco_missing_and_pom_untouched(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(tmp_path / "p", pom_text=pom(jacoco=False))
    before = (root / "pom.xml").read_bytes()
    result = build_unit_test_plan(root, target_modules=("",), search_path=fake.search_path)
    assert result.status is PlanStatus.SETUP_REQUIRED
    assert any("ayri exact onay" in step for step in result.setup_plan)
    assert (root / "pom.xml").read_bytes() == before  # sessiz POM degisikligi yok


def test_replay_non_maven_and_unknown_module_are_not_supported(tmp_path: Path) -> None:
    gradle = tmp_path / "g"
    write(gradle / "build.gradle", "plugins { id 'java' }")
    assert build_unit_test_plan(gradle, target_modules=("",)).status is PlanStatus.NOT_SUPPORTED
    root = make_project(tmp_path / "p")
    result = build_unit_test_plan(root, target_modules=("nope",))
    assert result.status is PlanStatus.NOT_SUPPORTED


def test_replay_unknown_engine_makes_plan_not_ready(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(tmp_path / "p", pom_text=pom(deps=""))
    result = build_unit_test_plan(root, target_modules=("",), search_path=fake.search_path)
    assert result.status is PlanStatus.SETUP_REQUIRED
    assert any("JUnit4/Platform dogrulanamadi" in r for r in result.reasons)


# ------------------------------------------------------------------ wrapper


def make_wrapper(root: Path, fake: FakeMaven, *, url: bool = True) -> Path:
    name = "mvnw"
    wrapper = fake.install(root, name)
    props = "distributionUrl=https://repo.example.invalid/maven.zip\n" if url else "x=1\n"
    write(root / ".mvn/wrapper/maven-wrapper.properties", props)
    return wrapper


def test_replay_wrapper_needs_download_permission_else_falls_back_to_installed(
    tmp_path: Path,
) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(tmp_path / "p")
    make_wrapper(root, fake)
    plain = build_unit_test_plan(root, target_modules=("",), search_path=fake.search_path)
    assert plain.plan is not None and plain.plan.launcher.kind is LauncherKind.INSTALLED
    assert any("ag gerektirebilir" in note for note in plain.reasons)
    allowed = build_unit_test_plan(
        root, target_modules=("",), allow_wrapper_download=True, search_path=fake.search_path
    )
    assert allowed.plan is not None and allowed.plan.launcher.kind is LauncherKind.WRAPPER
    assert allowed.plan.launcher.distribution_url == "https://repo.example.invalid/maven.zip"
    # wrapper var, indirme izni yok, kurulu mvn yok -> tool-missing
    nothing = build_unit_test_plan(root, target_modules=("",), search_path=str(tmp_path / "none"))
    assert nothing.status is PlanStatus.TOOL_MISSING


def test_replay_wrapper_drift_is_detected(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(tmp_path / "p")
    wrapper = make_wrapper(root, fake)
    plan = build_unit_test_plan(
        root, target_modules=("",), allow_wrapper_download=True, search_path=fake.search_path
    ).plan
    assert plan is not None
    verify_launcher(plan.launcher)
    props = root / ".mvn/wrapper/maven-wrapper.properties"
    props.write_text("distributionUrl=https://evil.example.invalid/m.zip\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="drift"):
        verify_launcher(plan.launcher)
    props.write_text("distributionUrl=https://repo.example.invalid/maven.zip\n", encoding="utf-8")
    verify_launcher(plan.launcher)
    wrapper.write_text(
        wrapper.read_text(encoding="utf-8") + "\r\nrem tampered\r\n", encoding="utf-8"
    )
    with pytest.raises(PolicyViolation, match="drift"):
        verify_launcher(plan.launcher)


# ------------------------------------------------------------------ yetki / execution kapisi


def test_replay_effective_pom_and_probe_are_gated_classes(tmp_path: Path) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(tmp_path / "p")
    effective = build_effective_pom_plan(root, search_path=fake.search_path).plan
    probe = build_version_probe_plan(root, search_path=fake.search_path).plan
    assert effective is not None and probe is not None
    assert effective.execution_class is ExecutionClass.PLUGIN_EXECUTION
    assert probe.execution_class is ExecutionClass.TOOL_PROBE and probe.args == ("-v",)
    assert effective.args == ("-B", "help:effective-pom")
    with pytest.raises(PolicyViolation, match="yetkisi yok"):
        check_authorization(effective, None)
    probe_auth = ExecutionAuthorization(probe.plan_digest, ExecutionClass.TOOL_PROBE)
    with pytest.raises(PolicyViolation, match="digest"):
        check_authorization(effective, probe_auth)  # baska plana ait yetki
    wrong_class = ExecutionAuthorization(effective.plan_digest, ExecutionClass.TOOL_PROBE)
    with pytest.raises(PolicyViolation, match="sinifi"):
        check_authorization(effective, wrong_class)
    no_network = ExecutionAuthorization(effective.plan_digest, ExecutionClass.PLUGIN_EXECUTION)
    with pytest.raises(PolicyViolation, match="ag"):
        check_authorization(effective, no_network)
    check_authorization(
        effective,
        ExecutionAuthorization(effective.plan_digest, ExecutionClass.PLUGIN_EXECUTION, True),
    )
    check_authorization(probe, probe_auth)


def test_replay_parse_version_output() -> None:
    assert parse_version_output("Apache Maven 3.9.9 (abc)\nJava version: 21.0.1, vendor: X\n") == (
        "3.9.9",
        "21.0.1",
    )
    assert parse_version_output("garbage") == (None, None)


# ---------------------------------------------- shell metni / traversal / link (M11)


@pytest.mark.parametrize(
    "bad", ["A;calc", "A&&dir", "A|B", "$(id)", "A B", "`x`", "A%PATH%", 'A"B']
)
def test_replay_free_shell_text_is_rejected_in_selection_and_args(tmp_path: Path, bad: str) -> None:
    fake = FakeMaven(tmp_path / "fake")
    root = make_project(tmp_path / "p")
    with pytest.raises(ValidationFailed):
        build_unit_test_plan(
            root, target_modules=("",), test_selection=(bad,), search_path=fake.search_path
        )
    result = build_unit_test_plan(root, target_modules=("",), search_path=fake.search_path)
    assert result.plan is not None
    with pytest.raises(ValidationFailed):
        MavenPlan(
            **{
                **{f: getattr(result.plan, f) for f in result.plan.__dataclass_fields__},
                "args": ("test", bad),
            }
        )


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/abs", "C:/x", "a\\b", "a//b", "a:b", ""])
def test_replay_resolve_inside_rejects_traversal(tmp_path: Path, bad: str) -> None:
    if bad == "":
        assert resolve_inside(tmp_path, bad) == tmp_path.resolve()
        return
    with pytest.raises((PolicyViolation, ValidationFailed)):
        resolve_inside(tmp_path, bad)


def make_link(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False
        )
        if done.returncode == 0:
            return
    pytest.skip("symlink/junction olusturulamadi")


def test_replay_symlink_or_junction_module_and_source_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside / "pom.xml", pom(artifact="evil"))
    write(outside / "src/main/java/p/A.java", "class A {}")
    root = make_project(
        tmp_path / "p", pom_text=pom(artifact="parent", packaging="pom", modules=("link",))
    )
    make_link(outside, root / "link")
    with pytest.raises(PolicyViolation):
        discover_project(root)
    with pytest.raises(PolicyViolation):
        resolve_inside(root, "link/src/main/java/p/A.java")
