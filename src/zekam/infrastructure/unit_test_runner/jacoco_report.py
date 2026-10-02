"""JaCoCo XML raporu -> modelden bagimsiz ``CoverageObservation`` (W04, M01-M03, M06, M16).

Kurallar:

- LINE/BRANCH icin yalniz ``sourcefile`` sayaclari esastir; class/method sayaclari
  toplanmaz (ayni satir cok metot/inner class'ta cift sayilmaz).
- ``line`` elemanlarindan turetilen sayac (ci>0 -> covered, ci==0&&mi>0 -> missed) raporun
  sourcefile sayaciyla capraz kontrol edilir; kismi satir (ci>0, mi>0) covered sayilir.
- Sayacin yoklugu tek basina 0 degildir: branch yoksa NOT_APPLICABLE, debug/line bilgisi
  eksikse MAPPING_ERROR, kayit hic yoksa MISSING_REPORT.
- Modul kimligi raporun icinden degil cagirandan gelir (modul basina bir rapor); ayni
  basename farkli package/modulde ayrilir. ``group`` (aggregate) raporu desteklenmez.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from zekam.domain.errors import ValidationFailed
from zekam.domain.unit_test_engineering import (
    CoverageMetric,
    CoverageObservation,
    CoverageState,
    normalize_relative_source,
)
from zekam.infrastructure.unit_test_runner.safe_xml import (
    DEFAULT_MAX_BYTES,
    JACOCO_DOCTYPES,
    XmlElement,
    parse_xml_bounded,
)

DEFAULT_SOURCE_ROOTS: Final = ("src/main/java",)


@dataclass(frozen=True, slots=True)
class JacocoCounter:
    covered: int
    missed: int

    @property
    def total(self) -> int:
        return self.covered + self.missed


@dataclass(frozen=True, slots=True)
class JacocoLine:
    nr: int
    mi: int
    ci: int
    mb: int
    cb: int


@dataclass(frozen=True, slots=True)
class JacocoClass:
    package: str
    name: str
    sourcefilename: str | None
    instruction_total: int


@dataclass(frozen=True, slots=True)
class JacocoSourceFile:
    package: str
    name: str
    lines: tuple[JacocoLine, ...]
    line: JacocoCounter | None
    branch: JacocoCounter | None


@dataclass(frozen=True, slots=True)
class JacocoReport:
    name: str
    sourcefiles: tuple[JacocoSourceFile, ...]
    classes: tuple[JacocoClass, ...]
    grouped: bool
    session_count: int


def _int(attrs: Mapping[str, str], key: str) -> int:
    raw = attrs.get(key)
    if raw is None or not raw.isascii() or not raw.isdigit():
        raise ValidationFailed("JaCoCo sayac/satir degeri negatif olmayan tamsayi degil")
    return int(raw)


def _counters(element: XmlElement) -> dict[str, JacocoCounter]:
    result: dict[str, JacocoCounter] = {}
    for counter in element.find_all("counter"):
        kind = counter.attrs.get("type", "")
        if kind in result:
            raise ValidationFailed("JaCoCo ayni sayac tipi tekrar ediyor")
        result[kind] = JacocoCounter(
            covered=_int(counter.attrs, "covered"), missed=_int(counter.attrs, "missed")
        )
    return result


def parse_jacoco_report(data: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES) -> JacocoReport:
    root = parse_xml_bounded(data, max_bytes=max_bytes, allowed_doctypes=JACOCO_DOCTYPES)
    if root.tag != "report":
        raise ValidationFailed("JaCoCo raporu 'report' koku tasimali")
    grouped = bool(root.find_all("group"))
    sourcefiles: list[JacocoSourceFile] = []
    classes: list[JacocoClass] = []
    for package in root.find_all("package"):
        package_name = package.attrs.get("name")
        if package_name is None:
            raise ValidationFailed("JaCoCo package adi eksik")
        for cls in package.find_all("class"):
            name = cls.attrs.get("name")
            if not name:
                raise ValidationFailed("JaCoCo class adi eksik")
            instructions = _counters(cls).get("INSTRUCTION")
            classes.append(
                JacocoClass(
                    package_name,
                    name,
                    cls.attrs.get("sourcefilename"),
                    instructions.total if instructions else 0,
                )
            )
        for source in package.find_all("sourcefile"):
            name = source.attrs.get("name")
            if not name:
                raise ValidationFailed("JaCoCo sourcefile adi eksik")
            lines = tuple(
                JacocoLine(
                    _int(item.attrs, "nr"),
                    _int(item.attrs, "mi"),
                    _int(item.attrs, "ci"),
                    _int(item.attrs, "mb"),
                    _int(item.attrs, "cb"),
                )
                for item in source.find_all("line")
            )
            counters = _counters(source)
            sourcefiles.append(
                JacocoSourceFile(
                    package_name, name, lines, counters.get("LINE"), counters.get("BRANCH")
                )
            )
    return JacocoReport(
        root.attrs.get("name", ""),
        tuple(sourcefiles),
        tuple(classes),
        grouped,
        len(root.find_all("sessioninfo")),
    )


class TargetMappingError(ValidationFailed):
    """Hedef dosya module/source root/package'a kanitli sekilde eslenemedi."""


@dataclass(frozen=True, slots=True)
class JavaTarget:
    source_file: str
    module: str
    source_root: str
    package: str
    name: str


def resolve_java_target(
    source_file: str,
    *,
    modules: Sequence[str],
    source_roots: Mapping[str, Sequence[str]] | None = None,
) -> JavaTarget:
    """Proje-relative yolu module + source root + package + sourcefile'a esler.

    ``modules`` proje-relative modul dizinleridir (kok modul ``""``). Basename ile karar
    verilmez; en uzun modul oneki secilir ve source root onekini tasimayan yol reddedilir.
    """

    path = normalize_relative_source(source_file)
    if not path.endswith(".java"):
        raise TargetMappingError("Yalniz .java hedefleri desteklenir")
    candidates = sorted(
        (m for m in modules if m == "" or path.startswith(m + "/")), key=len, reverse=True
    )
    if candidates:
        module = candidates[0]  # en uzun modul eslesti; kaybederse ust module dusulmez
        remainder = path if module == "" else path[len(module) + 1 :]
        roots = (source_roots or {}).get(module, DEFAULT_SOURCE_ROOTS)
        for root in roots:
            if remainder.startswith(root + "/"):
                inner = remainder[len(root) + 1 :]
                package, _, name = inner.rpartition("/")
                return JavaTarget(path, module, root, package, name)
    raise TargetMappingError("Hedef hicbir modulun source root altinda degil")


def _measured(
    target: JavaTarget, metric: CoverageMetric, counter: JacocoCounter
) -> CoverageObservation:
    return CoverageObservation.measured(
        target.source_file, metric, covered=counter.covered, missed=counter.missed
    )


def _unmeasured(
    target: JavaTarget, metric: CoverageMetric, state: CoverageState, reason: str
) -> CoverageObservation:
    return CoverageObservation(target.source_file, metric, state, reason=reason)


def _both(
    target: JavaTarget, state: CoverageState, reason: str
) -> tuple[CoverageObservation, CoverageObservation]:
    return (
        _unmeasured(target, CoverageMetric.LINE, state, reason),
        _unmeasured(target, CoverageMetric.BRANCH, state, reason),
    )


def _branch_observation(target: JavaTarget, source: JacocoSourceFile) -> CoverageObservation:
    derived = JacocoCounter(
        covered=sum(item.cb for item in source.lines), missed=sum(item.mb for item in source.lines)
    )
    metric = CoverageMetric.BRANCH
    if source.branch is None:
        if derived.total > 0:
            return _unmeasured(
                target, metric, CoverageState.MAPPING_ERROR, "line elemaninda branch var, sayac yok"
            )
        return _unmeasured(target, metric, CoverageState.NOT_APPLICABLE, "dosyada branch yok")
    if source.branch.total == 0 or source.branch != derived:
        return _unmeasured(
            target,
            metric,
            CoverageState.MAPPING_ERROR,
            "BRANCH sayaci ile line elemanlari uyumsuz",
        )
    return _measured(target, metric, source.branch)


def observe_target(
    target: JavaTarget, report: JacocoReport | None
) -> tuple[CoverageObservation, CoverageObservation]:
    """Bir hedef icin (LINE, BRANCH) gozlemleri; sahte 0/100 uretmez."""

    if report is None:
        return _both(target, CoverageState.MISSING_REPORT, "modul JaCoCo raporu yok")
    if report.grouped:
        return _both(
            target, CoverageState.MAPPING_ERROR, "group/aggregate JaCoCo raporu desteklenmiyor"
        )
    key = (target.package, target.name)
    matches = [s for s in report.sourcefiles if (s.package, s.name) == key]
    if len(matches) > 1:
        return _both(target, CoverageState.MAPPING_ERROR, "ayni package/sourcefile tekrar ediyor")
    instructions = sum(
        c.instruction_total
        for c in report.classes
        if c.package == target.package and c.sourcefilename == target.name
    )
    has_class = any(
        c.package == target.package and c.sourcefilename == target.name for c in report.classes
    )
    if not matches:
        if not has_class:
            return _both(target, CoverageState.MISSING_REPORT, "raporda sourcefile/class yok")
        if instructions > 0:
            return _both(
                target,
                CoverageState.MAPPING_ERROR,
                "class kaydi var ama sourcefile/line bilgisi yok (debug bilgisi eksik)",
            )
        return _both(target, CoverageState.NOT_APPLICABLE, "calistirilabilir instruction yok")

    source = matches[0]
    numbers = [item.nr for item in source.lines]
    if len(set(numbers)) != len(numbers):
        return _both(target, CoverageState.MAPPING_ERROR, "ayni satir birden cok kez raporlanmis")
    derived_line = JacocoCounter(
        covered=sum(1 for item in source.lines if item.ci > 0),
        missed=sum(1 for item in source.lines if item.ci == 0 and item.mi > 0),
    )
    if source.line is None:
        if source.lines:
            return _both(target, CoverageState.MAPPING_ERROR, "line elemani var, LINE sayaci yok")
        if instructions > 0:
            return _both(
                target, CoverageState.MAPPING_ERROR, "LINE sayaci yok (debug/line bilgisi eksik)"
            )
        return _both(target, CoverageState.NOT_APPLICABLE, "calistirilabilir satir yok")
    if source.line.total == 0 or source.line != derived_line:
        return _both(target, CoverageState.MAPPING_ERROR, "LINE sayaci ile line elemanlari uyumsuz")
    return _measured(target, CoverageMetric.LINE, source.line), _branch_observation(target, source)


def build_observations(
    source_files: Sequence[str],
    *,
    modules: Sequence[str],
    reports: Mapping[str, JacocoReport],
    source_roots: Mapping[str, Sequence[str]] | None = None,
) -> tuple[CoverageObservation, ...]:
    """Istek dosyalari icin (LINE, BRANCH) gozlemleri; eslenemeyen hedef MAPPING_ERROR."""

    result: list[CoverageObservation] = []
    for source_file in source_files:
        try:
            target = resolve_java_target(source_file, modules=modules, source_roots=source_roots)
        except TargetMappingError as exc:
            for metric in (CoverageMetric.LINE, CoverageMetric.BRANCH):
                result.append(
                    CoverageObservation(
                        source_file, metric, CoverageState.MAPPING_ERROR, reason=str(exc)
                    )
                )
            continue
        result.extend(observe_target(target, reports.get(target.module)))
    return tuple(result)
