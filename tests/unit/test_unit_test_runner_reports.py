"""W04: guvenli XML, JaCoCo ve Surefire rapor parser'lari (replay fixture; gercek Maven yok).

Fixture'lar elle yazilmis JaCoCo/Surefire XML'idir; gercek bir Maven kosusundan
uretilmemistir ve bu testler "replay" olarak etiketlidir.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from zekam.domain.errors import ValidationFailed
from zekam.domain.unit_test_engineering import (
    CoverageMetric,
    CoveragePolicy,
    CoverageState,
    EvaluationVerdict,
    UnitTestBudget,
    UnitTestRequest,
    evaluate_scope,
)
from zekam.infrastructure.unit_test_runner.jacoco_report import (
    TargetMappingError,
    build_observations,
    parse_jacoco_report,
    resolve_java_target,
)
from zekam.infrastructure.unit_test_runner.safe_xml import parse_xml_bounded
from zekam.infrastructure.unit_test_runner.surefire_report import (
    TestRunStatus,
    evaluate_modules,
    summarize,
)

DOCTYPE = '<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">'


def counter(kind: str, covered: int, missed: int) -> str:
    return f'<counter type="{kind}" missed="{missed}" covered="{covered}"/>'


def line(nr: int, mi: int, ci: int, mb: int = 0, cb: int = 0) -> str:
    return f'<line nr="{nr}" mi="{mi}" ci="{ci}" mb="{mb}" cb="{cb}"/>'


def report(*packages: str, extra: str = "") -> bytes:
    body = "".join(packages)
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>{DOCTYPE}'
        f'<report name="m">{extra}{body}</report>'
    ).encode()


def package(name: str, *members: str) -> str:
    return f'<package name="{name}">{"".join(members)}</package>'


def klass(name: str, source: str | None, instructions: int = 10, *extra: str) -> str:
    attr = f' sourcefilename="{source}"' if source else ""
    instr = counter("INSTRUCTION", instructions, 0)
    return f'<class name="{name}"{attr}>{instr}{"".join(extra)}</class>'


def sourcefile(name: str, lines: list[str], *counters: str) -> str:
    return f'<sourcefile name="{name}">{"".join(lines)}{"".join(counters)}</sourcefile>'


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def blocked(*_a: object, **_k: object) -> None:
        raise AssertionError("parser ag erisimi denedi")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    yield


# ------------------------------------------------------------------ safe_xml (M16)


def test_replay_safe_xml_reads_plain_document() -> None:
    root = parse_xml_bounded(b"<a x='1'><b>t</b></a>")
    assert root.attrs == {"x": "1"} and root.children[0].text == "t"


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>',
        b'<!DOCTYPE r [<!ENTITY x SYSTEM "http://127.0.0.1:1/x">]><r>&x;</r>',
        b'<!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><r>&b;</r>',
        b'<!DOCTYPE r SYSTEM "http://127.0.0.1:1/evil.dtd"><r/>',
        b'<!DOCTYPE r PUBLIC "-//X//Y//EN" "report.dtd"><r/>',
        DOCTYPE.encode() + b"<report>&undefined;</report>",
        b'<!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd" [<!ENTITY q "z">]>'
        b"<report/>",
        b"<a><b></a>",
        b"",
        b"<a/><b/>",
        b"not xml",
    ],
)
def test_replay_safe_xml_rejects_unsafe_or_malformed(payload: bytes) -> None:
    with pytest.raises(ValidationFailed):
        parse_xml_bounded(payload, allowed_doctypes=frozenset())
    # JaCoCo DOCTYPE izni verilse bile entity/harici/bozuk girdi reddedilir (izinli
    # sadece sabit report.dtd; ic altkume veya entity yok).
    with pytest.raises(ValidationFailed):
        parse_xml_bounded(
            payload, allowed_doctypes=frozenset({("-//JACOCO//DTD Report 1.1//EN", "report.dtd")})
        )


def test_replay_safe_xml_enforces_size_depth_and_node_limits() -> None:
    with pytest.raises(ValidationFailed, match="boyut"):
        parse_xml_bounded(b"<a>" + b"x" * 100 + b"</a>", max_bytes=50)
    with pytest.raises(ValidationFailed, match="derinlik"):
        parse_xml_bounded(b"<a>" * 20 + b"</a>" * 20, max_depth=10)
    with pytest.raises(ValidationFailed, match="eleman"):
        parse_xml_bounded(b"<a>" + b"<b/>" * 50 + b"</a>", max_nodes=10)


# ------------------------------------------------------------------ JaCoCo


def test_replay_jacoco_sourcefile_counter_is_used_not_class_sums_m02_m03() -> None:
    # Satir 10 iki metot/inner class tarafindan paylasilir; satir 11 kismi (ci>0, mi>0).
    lines = [line(10, 0, 4, 1, 1), line(11, 2, 3, 0, 2), line(12, 3, 0), line(13, 0, 1)]
    xml = report(
        package(
            "com/acme",
            klass("com/acme/Service", "Service.java", 20, counter("LINE", 3, 1)),
            klass("com/acme/Service$Inner", "Service.java", 6, counter("LINE", 2, 1)),
            sourcefile("Service.java", lines, counter("LINE", 3, 1), counter("BRANCH", 3, 1)),
        )
    )
    parsed = parse_jacoco_report(xml)
    line_obs, branch_obs = build_observations(
        ["src/main/java/com/acme/Service.java"], modules=[""], reports={"": parsed}
    )
    assert (line_obs.state, line_obs.covered, line_obs.missed, line_obs.total) == (
        CoverageState.MEASURED,
        3,
        1,
        4,
    )
    assert (branch_obs.covered, branch_obs.missed) == (3, 1)


def test_replay_jacoco_partial_line_counted_covered_and_mismatch_is_mapping_error() -> None:
    partial_only = [line(5, 7, 1)]  # kismi satir: LINE'da covered
    ok = report(package("p", sourcefile("A.java", partial_only, counter("LINE", 1, 0))))
    obs = build_observations(
        ["src/main/java/p/A.java"], modules=[""], reports={"": parse_jacoco_report(ok)}
    )
    assert (obs[0].covered, obs[0].missed) == (1, 0)
    assert obs[1].state is CoverageState.NOT_APPLICABLE  # branch yok -> N/A, 0 degil
    # ci>0 kismi satiri missed sayan sahte sayac: capraz kontrol reddeder.
    lying = report(package("p", sourcefile("A.java", partial_only, counter("LINE", 0, 1))))
    obs = build_observations(
        ["src/main/java/p/A.java"], modules=[""], reports={"": parse_jacoco_report(lying)}
    )
    assert {o.state for o in obs} == {CoverageState.MAPPING_ERROR}


def test_replay_jacoco_same_basename_different_package_and_module_m01() -> None:
    mod_a = report(
        package("com/x", sourcefile("Service.java", [line(1, 0, 1)], counter("LINE", 1, 0))),
        package(
            "com/y",
            sourcefile("Service.java", [line(1, 1, 0), line(2, 1, 0)], counter("LINE", 0, 2)),
        ),
    )
    mod_b = report(
        package("com/x", sourcefile("Service.java", [line(1, 1, 0)], counter("LINE", 0, 1))),
    )
    files = [
        "a/src/main/java/com/x/Service.java",
        "a/src/main/java/com/y/Service.java",
        "b/src/main/java/com/x/Service.java",
    ]
    obs = build_observations(
        files,
        modules=["", "a", "b"],
        reports={"a": parse_jacoco_report(mod_a), "b": parse_jacoco_report(mod_b)},
    )
    lines = {o.source_file: (o.covered, o.missed) for o in obs if o.metric is CoverageMetric.LINE}
    assert lines == {files[0]: (1, 0), files[1]: (0, 2), files[2]: (0, 1)}


def test_replay_jacoco_missing_mapping_and_not_applicable_states_m06() -> None:
    xml = report(
        package(
            "p",
            klass("p/NoDebug", "NoDebug.java", 12),  # instruction var, sourcefile/line yok
            klass("p/Iface", "Iface.java", 0),  # calistirilabilir kod yok
            klass("p/Half", "Half.java", 5),
            sourcefile(
                "Half.java",
                [],
            ),  # LINE sayaci ve line yok ama instruction var
            sourcefile("Dup.java", [line(1, 0, 1)], counter("LINE", 1, 0)),
            sourcefile("Dup.java", [line(1, 0, 1)], counter("LINE", 1, 0)),
            sourcefile("Skew.java", [line(1, 0, 1, 2, 0)], counter("LINE", 1, 0)),
        )
    )
    parsed = parse_jacoco_report(xml)
    names = ["NoDebug", "Iface", "Half", "Dup", "Skew", "Absent"]
    obs = build_observations(
        [f"src/main/java/p/{n}.java" for n in names], modules=[""], reports={"": parsed}
    )
    by = {(o.source_file.rsplit("/", 1)[1], o.metric): o for o in obs}
    assert by[("NoDebug.java", CoverageMetric.LINE)].state is CoverageState.MAPPING_ERROR
    assert by[("Iface.java", CoverageMetric.LINE)].state is CoverageState.NOT_APPLICABLE
    assert by[("Half.java", CoverageMetric.LINE)].state is CoverageState.MAPPING_ERROR
    assert by[("Dup.java", CoverageMetric.LINE)].state is CoverageState.MAPPING_ERROR
    # LINE tutarli, ama satirda branch var ve BRANCH sayaci yok -> BRANCH mapping error
    assert by[("Skew.java", CoverageMetric.LINE)].state is CoverageState.MEASURED
    assert by[("Skew.java", CoverageMetric.BRANCH)].state is CoverageState.MAPPING_ERROR
    assert by[("Absent.java", CoverageMetric.LINE)].state is CoverageState.MISSING_REPORT
    assert all(o.covered is None for o in obs if o.state is not CoverageState.MEASURED)


def test_replay_jacoco_missing_module_report_group_and_unmappable_targets() -> None:
    grouped = parse_jacoco_report(report(extra="<group name='g'></group>"))
    assert grouped.grouped
    obs = build_observations(
        ["m/src/main/java/p/A.java", "x/src/main/java/p/A.java", "src/main/java/p/B.txt"],
        modules=["", "m"],
        reports={"m": grouped},
    )
    states = [o.state for o in obs]
    assert states[:2] == [CoverageState.MAPPING_ERROR] * 2  # grouped rapor
    # x/... koku modul root'u altinda degil (kok modulun source root'u src/main/java)
    assert states[2:4] == [CoverageState.MAPPING_ERROR] * 2
    assert states[4:] == [CoverageState.MAPPING_ERROR] * 2  # .java degil
    missing = build_observations(["src/main/java/p/A.java"], modules=[""], reports={})
    assert {o.state for o in missing} == {CoverageState.MISSING_REPORT}


def test_replay_resolve_target_uses_longest_module_and_custom_source_root() -> None:
    target = resolve_java_target(
        "svc/api/src/main/java/com/acme/Svc.java", modules=["", "svc", "svc/api"]
    )
    assert (target.module, target.package, target.name) == ("svc/api", "com/acme", "Svc.java")
    custom = resolve_java_target(
        "svc/src/java/Top.java", modules=["svc"], source_roots={"svc": ["src/java"]}
    )
    assert (custom.package, custom.name) == ("", "Top.java")
    with pytest.raises(TargetMappingError):
        resolve_java_target("svc/api/src/test/java/X.java", modules=["svc", "svc/api"])


def test_replay_jacoco_malformed_values_and_wrong_root_rejected() -> None:
    bad_counter = report(
        package("p", sourcefile("A.java", [], '<counter type="LINE" missed="-1" covered="1"/>'))
    )
    with pytest.raises(ValidationFailed):
        parse_jacoco_report(bad_counter)
    with pytest.raises(ValidationFailed):
        parse_jacoco_report(b"<notreport/>")
    with pytest.raises(ValidationFailed):
        parse_jacoco_report(report(), max_bytes=10)


def test_replay_scope_evaluation_never_passes_with_mapping_error_or_rounding_m05() -> None:
    budget = UnitTestBudget(3, 60, 600)
    request = UnitTestRequest.with_defaults(
        project_id="p1",
        source_binding_id="b1",
        source_revision="rev",
        source_files=["src/main/java/p/A.java"],
        percent="90",
        budget=budget,
        policy=CoveragePolicy.PER_FILE,
    )
    near = report(
        package(
            "p",
            sourcefile(
                "A.java",
                [line(i, 0, 1) for i in range(1, 896)] + [line(i, 1, 0) for i in range(896, 1001)],
                counter("LINE", 895, 105),
            ),
        )
    )  # 89.5% -> gosterimde 90'a yuvarlanabilir
    obs = build_observations(
        request.source_files, modules=[""], reports={"": parse_jacoco_report(near)}
    )
    assert evaluate_scope(request, obs).verdict is EvaluationVerdict.NOT_MET
    broken = build_observations(request.source_files, modules=[""], reports={})
    assert evaluate_scope(request, broken).verdict is EvaluationVerdict.INCONCLUSIVE


# ------------------------------------------------------------------ Surefire (U06)


def suite(
    name: str,
    cases: str,
    tests: int,
    failures: int = 0,
    errors: int = 0,
    skipped: int = 0,
    props: str = "",
) -> bytes:
    return (
        f'<testsuite name="{name}" tests="{tests}" errors="{errors}" skipped="{skipped}" '
        f'failures="{failures}"><properties>{props}</properties>{cases}</testsuite>'
    ).encode()


def case(cls: str, name: str, child: str = "") -> str:
    return f'<testcase classname="{cls}" name="{name}">{child}</testcase>'


def test_replay_surefire_passed_counts_and_jvm_version() -> None:
    xml = suite(
        "com.acme.ATest",
        case("com.acme.ATest", "a") + case("com.acme.ATest", "b"),
        2,
        props=(
            '<property name="java.version" value="21.0.1"/>'
            '<property name="user.password" value="x"/>'
        ),
    )
    result = summarize([xml])
    assert (result.discovered, result.executed, result.passed) == (2, 2, 2)
    assert result.status is TestRunStatus.PASSED
    assert result.jvm == (("java.version", "21.0.1"),)  # yalniz allowlist property


def test_replay_surefire_no_tests_and_all_skipped_are_not_acceptable_u06() -> None:
    assert summarize([]).status is TestRunStatus.NO_TESTS
    assert summarize([suite("E", "", 0)]).status is TestRunStatus.NO_TESTS
    skipped = suite("S", case("S", "a", "<skipped/>") + case("S", "b", "<skipped/>"), 2, skipped=2)
    result = summarize([skipped])
    assert result.status is TestRunStatus.ALL_SKIPPED and result.executed == 0


def test_replay_surefire_failed_error_aborted_and_precedence() -> None:
    xml = suite(
        "F",
        case("F", "ok")
        + case("F", "fail", "<failure message='x'/>")
        + case("F", "err", "<error type='java.lang.Error'/>")
        + case("F", "assume", "<skipped type='org.opentest4j.TestAbortedException'/>")
        + case("F", "ign", "<skipped/>"),
        5,
        failures=1,
        errors=1,
        skipped=2,
    )
    result = summarize([xml])
    assert (result.failed, result.errors, result.aborted, result.skipped) == (1, 1, 1, 1)
    assert result.executed == 3 and result.status is TestRunStatus.FAILED


def test_replay_surefire_inconsistent_flaky_integration_and_malformed() -> None:
    liar = suite("L", case("L", "a"), 5)
    assert summarize([liar]).status is TestRunStatus.INCONSISTENT_REPORT
    flaky = suite("K", case("K", "a", "<flakyFailure/>"), 1)
    assert summarize([flaky]).status is TestRunStatus.FLAKY_SUSPECTED
    mixed = suite("IT", case("com.acme.OrderIT", "a") + case("com.acme.UnitTest", "b"), 2)
    assert summarize([mixed]).status is TestRunStatus.INTEGRATION_TESTS_PRESENT
    for bad in (b"<x/>", b"<testsuite", b'<!DOCTYPE t [<!ENTITY a "b">]><testsuite/>'):
        with pytest.raises(ValidationFailed):
            summarize([bad])


def test_replay_surefire_target_module_without_tests_is_fail_closed_non_target_ok() -> None:
    summaries = {"util": summarize([])}
    statuses = evaluate_modules(summaries, target_modules=["svc"])
    assert statuses == {"svc": TestRunStatus.NO_TESTS}
    assert "util" not in statuses  # test icermeyen utility/parent modul build hatasi degil
