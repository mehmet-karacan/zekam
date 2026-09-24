"""AC-05: Doctor cognitive health kontrolleri drift/hygiene tespiti ve read-only.

Cognitive check'ler (memory/knowledge/skill/continuity/context/learning) kanonik
SQLite store'larini valniz `mode=ro` baglantiyla okur; default doctor yolu mutasyon
yapmaz ve hicbir destructive implicit repair uretmez. Bu test:

- bir cognitive check'in finding urettigi (memory orphan drift) durumunu kurar,
- default doctor yolunun read-only oldugunu (kayit dosyalari degismez) dogrular,
- check calistirinca hicbir sey silinmedigini/kayit degismedigini dogrular.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zekam.application.composition import ApplicationContext, build_context, build_doctor_checks
from zekam.application.diagnostics import CheckResult, CheckStatus
from zekam.domain.canonical import canonical_json, digest
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.local_learning import SQLiteLocalLearning

pytestmark = pytest.mark.integration


def _build_home(tmp_path: Path) -> Path:
    """Operational + learning (ve diger additive store'lari) hazirlar.

    Operational v3 schema bootstrap edilir; learning/extended store'lar
    LocalCoreServices.bootstrap_extensions ile uretimdeki private dizin
    kurallariyla olusturulur.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    operational = home / "state" / "operational.db"
    operational.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    operational_schema.bootstrap(operational)
    operational.chmod(0o600)

    context = build_context(home=home, environ={})
    LocalCoreServices.from_context(context).bootstrap_extensions()
    return home


def _seed_orphan_memory(tmp_path: Path) -> None:
    """Learning store'a, hicbir revision'a bagli olmayan bir bellek adayi koyar.

    MemoryHygieneCheck'in 'cognitive.memory-orphan-candidate' finding uretmesini
    garantiler (en az bir cognitive check'in finding urettigi drift durumu).
    """
    learning = SQLiteLocalLearning(
        (tmp_path / "state" / "learning.db").resolve(),
        operational_path=(tmp_path / "state" / "operational.db").resolve(),
    )
    row = (
        digest("orphan-candidate"),
        "orphan-001",
        digest("scope"),
        "semantic",
        digest("content"),
        "test",
        "author-a",
        "2026-09-24T12:00:00+00:00",
        canonical_json({"text": "orphan memory candidate"}),
    )
    with sqlite3.connect(learning.path) as db:
        db.execute("pragma foreign_keys=on")
        db.execute(
            "insert into memory_candidate(candidate_digest,candidate_id,scope_digest,"
            "memory_class,content_digest,source_kind,author_ref,observed_at,body_json) "
            "values(?,?,?,?,?,?,?,?,?)",
            row,
        )
        db.commit()


def _db_identity(paths: list[Path]) -> dict[str, int]:
    identity: dict[str, int] = {}
    for path in paths:
        identity[str(path)] = path.read_bytes().__len__() if path.exists() else -1
    return identity


def _cognitive_results(context: ApplicationContext) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in build_doctor_checks(context):
        if check.category == "cognitive":
            results.append(check.run())
    return results


def test_cognitive_check_detects_memory_drift(tmp_path: Path) -> None:
    home = _build_home(tmp_path)
    _seed_orphan_memory(tmp_path / "home")
    context = build_context(home=home, environ={})

    results = _cognitive_results(context)
    # Kategori kayitli ve coginitif check'ler donuyor.
    assert results, "cognitive kategorisinde hic check calismadi"

    orphan = next((r for r in results if r.check_id == "cognitive.memory"), None)
    assert orphan is not None
    assert orphan.status is CheckStatus.DEGRADED
    assert any(
        f.code == "cognitive.memory-orphan-candidate" for f in orphan.findings
    ), "orphan bellek adayi checking finding uretmesi beklenir"


def test_doctor_default_path_is_read_only(tmp_path: Path) -> None:
    home = _build_home(tmp_path)
    _seed_orphan_memory(tmp_path / "home")
    context = build_context(home=home, environ={})

    tracked = sorted(
        (home / "state").glob("*.db"),
        key=lambda p: str(p),
    )
    before = _db_identity(tracked)

    for result in _cognitive_results(context):
        # CheckResult read-only sozlesmeyi korur; authority uretmez.
        assert result.evidence.get("grants_authority") is not True
        for finding in result.findings:
            assert finding.authority_required is False

    after = _db_identity(tracked)
    assert after == before, "doctor check'leri kanonik kayit dosyalarini degistirdi"


def test_doctor_has_no_destructive_implicit_repair(tmp_path: Path) -> None:
    home = _build_home(tmp_path)
    _seed_orphan_memory(tmp_path / "home")
    context = build_context(home=home, environ={})

    state_dir = home / "state"
    before_files = {
        str(p): p.read_bytes() for p in state_dir.glob("*.db")
    }

    for _ in _cognitive_results(context):
        pass

    after_files = {
        str(p): p.read_bytes() for p in state_dir.glob("*.db")
    }
    # Hicbir row silinmedi / dosya icerigi degismedi; yeni dosya da olusmadi.
    assert set(before_files) == set(after_files)
    for key, payload in before_files.items():
        assert after_files[key] == payload, f"destructive degisiklik: {key}"


def test_cognitive_category_registered_in_composition(tmp_path: Path) -> None:
    home = _build_home(tmp_path)
    context = build_context(home=home, environ={})
    ids = {check.check_id for check in build_doctor_checks(context)}
    expected = {
        "cognitive.memory",
        "cognitive.knowledge",
        "cognitive.skill",
        "cognitive.continuity",
        "cognitive.context",
        "cognitive.learning",
    }
    assert expected.issubset(ids), f"eksik cognitive check: {expected - ids}"
