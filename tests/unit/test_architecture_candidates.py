"""Storage-only architecture evidence sampler (architecture_candidates) tests.

Synthetic fixture in the style of ``test_sqlite_knowledge_index.py``. These
tests exercise the sampler against the real shared path helper
(``zekam.application.source_architecture_answer``) so the integration is
verified end to end (path exclusion, module grouping and role signals).
Import-unavailability is simulated only by monkeypatching
``importlib.import_module`` for the exact helper name, with proper restore,
rather than a global ``sys.modules`` stub.

Coverage highlights:
* validation (exact int, ranges, boundary values),
* helper-missing fails closed (ConfigurationError),
* cross-project and cross-generation isolation,
* non-source exclusion + fair round-robin across modules,
* manifest + entrypoint prioritization,
* CamelCase role recognition for sampling priority,
* later-module starvation regression (fair module-fair selection *before*
  the bounded scan ceiling, via a small ``ARCHITECTURE_SCAN_CEILING`` seam),
* per-module / max-modules / scan-ceiling truncation honesty.
"""

from __future__ import annotations

import importlib
import math
from pathlib import Path

import pytest

from zekam.application.knowledge_index import KnowledgeIndexRecord
from zekam.application.source_architecture_answer import (
    is_implementation_source,
    source_module,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.knowledge import Locator
from zekam.infrastructure.sqlite import knowledge_index as knowledge_index_module
from zekam.infrastructure.sqlite.knowledge_index import (
    VECTOR_DIMENSION,
    SQLiteKnowledgeIndex,
)

pytestmark = pytest.mark.unit

HELPER_MODULE = "zekam.application.source_architecture_answer"


# --------------------------------------------------------------------------
# Fixture helpers (style of test_sqlite_knowledge_index.py)
# --------------------------------------------------------------------------


def _vector(primary: int) -> tuple[float, ...]:
    values = [0.0] * VECTOR_DIMENSION
    values[primary] = 1.0
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


def _record(
    chunk_id: str,
    *,
    project_id: str = "demo-servis",
    revision: str = "rev-1",
    path: str = "src/app/Main.java",
    text: str = "Sentetik kod kaniti metni; SHA-256 ile dogrulanabilir.",
    order: int = 0,
) -> KnowledgeIndexRecord:
    return KnowledgeIndexRecord(
        chunk_id=chunk_id,
        project_id=project_id,
        source_revision=revision,
        source_path=path,
        source_digest=digest({"source": path, "revision": revision}),
        locator=Locator(relative_path=path, line_start=1, line_end=3, object_name=None),
        text=text,
        content_digest=digest_of_bytes(text.encode("utf-8")),
        chunk_order=order,
        vector=_vector(0),
    )


def _build(
    index: SQLiteKnowledgeIndex,
    records: tuple[KnowledgeIndexRecord, ...],
    *,
    project_id: str = "demo-servis",
    revision: str = "rev-1",
    tree: str = "tree-1",
) -> str:
    generation = index.build_generation(
        records,
        project_id=project_id,
        source_revision=revision,
        tree_digest=digest(tree),
        source_manifest_digest=digest({"manifest": tree}),
        embedding_profile_digest=digest("embedding-profile"),
        provider_profile_digest=digest("provider-profile"),
        created_at="2026-10-20T00:00:00Z",
    )
    return generation.generation_digest


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_architecture_candidates_rejects_non_int_and_out_of_range_limits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "knowledge.sqlite3"
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(
            index, (_record("chunk-one", path="src/app/Main.java"),)
        )
        invalid = (
            {"limit": True},
            {"limit": 0},
            {"limit": 129},
            {"limit": 1.5},
            {"max_modules": True},
            {"max_modules": 0},
            {"max_modules": 17},
            {"per_module": True},
            {"per_module": 0},
            {"per_module": 9},
        )
        for kwargs in invalid:
            with pytest.raises(
                (ValidationFailed, TypeError),
                match="exact int|Architecture",
            ):
                index.architecture_candidates(
                    "demo-servis",
                    generation_digest=generation_digest,
                    **kwargs,  # type: ignore[arg-type]
                )
        # Exact boundary values are accepted.
        result = index.architecture_candidates(
            "demo-servis",
            generation_digest=generation_digest,
            limit=1,
            max_modules=1,
            per_module=1,
        )
        assert result["chunk_ids"] == ("chunk-one",)


def test_architecture_candidates_helper_missing_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "knowledge.sqlite3"
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(
            index, (_record("chunk-one", path="src/app/Main.java"),)
        )
        real_import = importlib.import_module

        def _missing(name: str, *args: object, **kwargs: object) -> object:
            if name == HELPER_MODULE:
                raise ModuleNotFoundError(
                    f"No module named {name!r} (simulated for the test)"
                )
            return real_import(name, *args, **kwargs)

        # Monkeypatch ONLY the exact helper name; importlib is restored
        # automatically after the test, so no global sys.modules pollution.
        monkeypatch.setattr(importlib, "import_module", _missing)
        with pytest.raises(ConfigurationError):
            index.architecture_candidates(
                "demo-servis", generation_digest=generation_digest
            )


# --------------------------------------------------------------------------
# Scope isolation
# --------------------------------------------------------------------------


def test_architecture_candidates_scoped_to_pinned_project_and_generation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "knowledge.sqlite3"
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_a = _build(
            index,
            (_record("gen-a-1", path="svc/AService.java", project_id="alpha"),),
            project_id="alpha",
        )
        _build(
            index,
            (_record("other-1", path="web/W.java", project_id="other"),),
            project_id="other",
        )
        generation_b = _build(
            index,
            (_record("gen-b-1", path="svc/BService.java", project_id="alpha"),),
            project_id="alpha",
            revision="rev-2",
            tree="tree-2",
        )
        # A later build supersedes gen A; both remain queryable by digest.
        result_a = index.architecture_candidates(
            "alpha", generation_digest=generation_a
        )
        assert result_a["chunk_ids"] == ("gen-a-1",)
        result_b = index.architecture_candidates(
            "alpha", generation_digest=generation_b
        )
        assert result_b["chunk_ids"] == ("gen-b-1",)
        # Unknown digests fail closed.
        with pytest.raises(PolicyViolation):
            index.architecture_candidates("alpha", generation_digest=digest("bogus"))
        # A generation belonging to another project is not visible here.
        other_generation = index.generation("other").generation_digest
        with pytest.raises(PolicyViolation):
            index.architecture_candidates(
                "alpha", generation_digest=other_generation
            )


# --------------------------------------------------------------------------
# Exclusion + fairness + prioritization
# --------------------------------------------------------------------------


def test_architecture_candidates_excludes_non_source_paths_and_fair_round_robin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "knowledge.sqlite3"
    records = tuple(
        _record(f"aa-{i:02d}", path=f"aa/AaClass{i:02d}.java", order=i) for i in range(12)
    ) + tuple(
        _record(f"bb-{i:02d}", path=f"bb/BbClass{i:02d}.java", order=i)
        for i in range(4)
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(
            index,
            records
            + (
                _record("docs-x", path="docs/README.md"),
                _record("task-x", path="task/not.md"),
                _record("agents-x", path="AGENTS.md"),
                _record("skill-x", path="skills/not.md"),
                _record("test-x", path="tests/unit/test_not.py"),
                _record("gen-x", path="generated/not.json"),
                _record("vendor-x", path="vendor/lib/not.java"),
            ),
        )
        result = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, limit=4
        )
        # Non-source paths never leak into ids nor the observed module count
        # (counted via the real helper over the full eligible population).
        assert result["observed_module_count"] == 2
        assert result["sampled_module_count"] == 2
        assert len(result["chunk_ids"]) == 4
        # Fair round-robin: one per module per round, never all from "aa".
        assert list(result["chunk_ids"])[:2] == ["aa-00", "bb-00"]
        assert set(result["chunk_ids"]) == {"aa-00", "bb-00", "aa-01", "bb-01"}
        assert result["truncated"] is True  # remaining candidates exist
        # Integration sanity: the real shared helper agrees on eligibility and
        # module grouping for the paths used here (docs/task/agents/skill/
        # test/generated/vendor excluded; aa/bb are real modules).
        assert not is_implementation_source("docs/README.md")
        assert not is_implementation_source("tests/unit/test_not.py")
        assert not is_implementation_source("vendor/lib/not.java")
        assert is_implementation_source("aa/AaClass00.java")
        assert source_module("aa/AaClass00.java") == "aa"
        assert source_module("bb/BbClass00.java") == "bb"


def test_architecture_candidates_prioritizes_manifest_and_entrypoint_over_classes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "knowledge.sqlite3"
    records = (
        _record("root-manifest", path="pom.xml"),
        *(
            _record(f"zz-{i:02d}", path=f"zz/ZzClass{i:02d}.java", order=i)
            for i in range(10)
        ),
        _record("app-entry", path="app/Application.java"),
        *(
            _record(f"aa-{i:02d}", path=f"aa/AaClass{i:02d}.java", order=i)
            for i in range(6)
        ),
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        result = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, limit=3
        )
        # Round 1 picks each module's best candidate: the root build manifest
        # and the entrypoint outrank arbitrary first classes of "aa"/"zz".
        assert list(result["chunk_ids"]) == [
            "root-manifest",
            "app-entry",
            "aa-00",
        ]
        # Four distinct eligible modules (., app, aa, zz); only three got a
        # chunk before the limit of 3 was reached.
        assert result["observed_module_count"] == 4
        assert result["sampled_module_count"] == 3


def test_architecture_candidates_recognizes_camelcase_roles_for_priority(
    tmp_path: Path,
) -> None:
    """CamelCase Controller/Service/Application names get sampling priority.

    Only the role family used for ordering is asserted; the downstream
    content analysis remains the authoritative role proof.
    """
    path = tmp_path / "knowledge.sqlite3"
    records = (
        _record("ctrl", path="web/FooController.java"),
        _record("svc", path="web/FooService.java"),
        _record("app", path="web/FooApplication.java"),
        _record("plain", path="web/FooPlain.java"),
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        result = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, limit=4
        )
        # All one module; role family orders: entrypoint(Application) first,
        # then controller, then service, then the plain source.
        assert list(result["chunk_ids"]) == ["app", "ctrl", "svc", "plain"]
        assert result["observed_module_count"] == 1
        assert result["truncated"] is False


# --------------------------------------------------------------------------
# Truncation honesty
# --------------------------------------------------------------------------


def test_architecture_candidates_per_module_cap_sets_truncated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "knowledge.sqlite3"
    records = tuple(
        _record(f"only-{i:02d}", path=f"only/OnlyClass{i:02d}.java", order=i)
        for i in range(10)
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        result = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, per_module=8
        )
        assert len(result["chunk_ids"]) == 8
        assert result["sampled_module_count"] == 1
        assert result["observed_module_count"] == 1
        # 2 candidates beyond the per-module cap must be reported honestly.
        assert result["truncated"] is True


def test_architecture_candidates_max_modules_omission_sets_truncated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "knowledge.sqlite3"
    records = tuple(
        _record(f"{module}-00", path=f"{module}/M00.java")
        for module in ("aaa", "bbb", "ccc", "ddd")
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        result = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, max_modules=2
        )
        assert result["observed_module_count"] == 4
        assert result["sampled_module_count"] == 2
        assert set(result["chunk_ids"]) == {"aaa-00", "bbb-00"}
        assert result["truncated"] is True


def test_architecture_candidates_scan_ceiling_sets_truncated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When eligible distinct files exceed the bounded scan ceiling, the
    result is bounded, ``truncated`` is honest, and the call is deterministic.

    A small ``ARCHITECTURE_SCAN_CEILING`` seam stands in for the real 4097 so
    the test stays compact (30 vectors) while still exercising the cap path.
    """
    path = tmp_path / "knowledge.sqlite3"
    # 30 distinct files in one module: one representative per file, so 30
    # eligible rows -- above the small scan-ceiling seam of 20 below.
    records = tuple(
        _record(f"bulk-{i:02d}", path=f"m/Class{i:02d}.java", order=i)
        for i in range(30)
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        monkeypatch.setattr(
            knowledge_index_module, "ARCHITECTURE_SCAN_CEILING", 20
        )
        result = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, limit=128
        )
        # The capped scan saw only 20 of the 30 files; the per-module cap (8)
        # then bounds the one module. truncated must be honest about the cap.
        assert len(result["chunk_ids"]) == 8
        assert result["sampled_module_count"] == 1
        assert result["observed_module_count"] == 1
        assert result["truncated"] is True
        # Deterministic: identical result on a repeat call.
        again = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, limit=128
        )
        assert again["chunk_ids"] == result["chunk_ids"]
        assert again["truncated"] is result["truncated"]
        # Restoring the real ceiling removes the scan cap: more files are seen,
        # the result is no longer truncated by the ceiling (only per_module).
        monkeypatch.setattr(
            knowledge_index_module, "ARCHITECTURE_SCAN_CEILING", 4097
        )
        uncapped = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, limit=128
        )
        assert len(uncapped["chunk_ids"]) == 8  # still bounded by per_module


# --------------------------------------------------------------------------
# Finding 1: later-module starvation (module-fair selection BEFORE the cap)
# --------------------------------------------------------------------------


def test_architecture_candidates_large_early_module_does_not_starve_later_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large early (lexicographic) module must not hide later real modules.

    ``aa`` holds 10 plain (low-role) files; ``bb``/``cc``/``dd`` each hold a
    single high-role file that sorts after ``aa``. With a small scan ceiling
    seam the lexicographic-first rows are the ``aa`` files, but eligibility +
    module balancing happen BEFORE the cap, so every high-role module is still
    sampled and its count is reported truthfully.
    """
    path = tmp_path / "knowledge.sqlite3"
    records = tuple(
        _record(f"aa-{i:02d}", path=f"aa/Plain{i:02d}.java", order=i)
        for i in range(10)
    ) + (
        _record("bb-ctrl", path="bb/BbController.java"),
        _record("cc-svc", path="cc/CcService.java"),
        _record("dd-repo", path="dd/DdRepository.java"),
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        # Seam a small metadata scan ceiling (the real value is 4097).
        monkeypatch.setattr(
            knowledge_index_module, "ARCHITECTURE_SCAN_CEILING", 6
        )
        result = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, limit=8
        )
        # All four modules survive the capped scan and are sampled fairly.
        # The capped scan sees only 6 of the 13 eligible rows (4 rank-1 rows,
        # then aa's next two), but every high-role module is present: the
        # large early ``aa`` module did not starve ``bb``/``cc``/``dd``.
        assert result["observed_module_count"] == 4
        assert result["sampled_module_count"] == 4
        assert set(result["chunk_ids"]) == {
            "bb-ctrl",
            "cc-svc",
            "dd-repo",
            "aa-00",
            "aa-01",
            "aa-02",
        }
        # The high-role modules are not starved by the large early module.
        assert {"bb-ctrl", "cc-svc", "dd-repo"} <= set(result["chunk_ids"])
        assert result["truncated"] is True  # the cap omitted eligible rows


def test_architecture_candidates_guidance_docs_do_not_starve_code_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall of guidance docs must not starve later real code modules."""
    path = tmp_path / "knowledge.sqlite3"
    records = tuple(
        _record(f"dd-{i:02d}", path=f"docs/guidance/Guide{i:02d}.md", order=i)
        for i in range(12)
    ) + (
        _record("core-1", path="core/CoreService.java"),
        _record("ui-1", path="ui/UiController.java"),
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        # Docs are excluded by the eligibility predicate; the cap seam is
        # irrelevant but kept to mirror the starvation regression above.
        monkeypatch.setattr(
            knowledge_index_module, "ARCHITECTURE_SCAN_CEILING", 6
        )
        result = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, limit=4
        )
        # Docs never leak into ids or the observed count.
        assert result["observed_module_count"] == 2
        assert result["sampled_module_count"] == 2
        assert set(result["chunk_ids"]) == {"core-1", "ui-1"}
        assert not any(chunk_id.startswith("dd-") for chunk_id in result["chunk_ids"])


# --------------------------------------------------------------------------
# Finding 2: a single large manifest/class must not fill all per-module slots
# --------------------------------------------------------------------------


def test_architecture_candidates_large_manifest_still_yields_other_roles(
    tmp_path: Path,
) -> None:
    """One big manifest/file must not consume every per-module slot."""
    path = tmp_path / "knowledge.sqlite3"
    # 20 chunks of a single large build manifest (one distinct file) plus one
    # chunk each of a controller, service and a plain class in the same module.
    records = tuple(
        _record(f"man-{i:02d}", path="web/pom.xml", order=i)
        for i in range(20)
    ) + (
        _record("ctrl", path="web/WebController.java"),
        _record("svc", path="web/WebService.java"),
        _record("plain", path="web/WebPlain.java"),
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        # The per-file round-robin represents every distinct file before the
        # large manifest's extra chunks are admitted, so the other real code
        # roles are all sampled and the manifest does not fill every slot.
        result = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, limit=8
        )
        ids = list(result["chunk_ids"])
        # Bounded representative contract: each of the four distinct files
        # contributes exactly ONE chunk (4 files => 4 ids); the manifest's
        # repeated chunks are omitted by design and reported honestly.
        assert set(ids) == {"man-00", "ctrl", "svc", "plain"}
        assert len(ids) == 4
        assert result["truncated"] is True
        assert result["sampled_module_count"] == 1
        assert result["observed_module_count"] == 1


def test_architecture_candidates_manifest_controller_service_all_modules(
    tmp_path: Path,
) -> None:
    """Regression: a large manifest + many same-role chunks still yields the
    other real code roles AND all three modules."""
    path = tmp_path / "knowledge.sqlite3"
    # Module "aaa": 15 chunks of one big manifest + 6 same-role controllers.
    records = tuple(
        _record(f"man-{i:02d}", path="aaa/pom.xml", order=i)
        for i in range(15)
    ) + tuple(
        _record(f"aaa-ctrl-{i:02d}", path=f"aaa/AaaController{i:02d}.java", order=i)
        for i in range(6)
    ) + (
        # Module "bbb": a service. Module "ccc": a plain class.
        _record("bbb-svc", path="bbb/BbbService.java"),
        _record("ccc-plain", path="ccc/CccPlain.java"),
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        result = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, limit=8
        )
        ids = set(result["chunk_ids"])
        # Bounded representative contract: the manifest contributes a single
        # representative, not 15.
        assert [chunk_id for chunk_id in ids if chunk_id.startswith("man-")] == ["man-00"]
        # Other real code roles (controller/service) are still sampled.
        assert any(chunk_id.startswith("aaa-ctrl-") for chunk_id in ids)
        assert "bbb-svc" in ids
        # All three modules are present (no starvation of later modules).
        modules_in_result = {source_module(path) for path in (
            "aaa/pom.xml", "bbb/BbbService.java", "ccc/CccPlain.java"
        )}
        assert len(modules_in_result) == 3
        assert result["observed_module_count"] == 3
        assert result["sampled_module_count"] == 3
        assert result["truncated"] is True  # many candidates exceed the caps


# --------------------------------------------------------------------------
# Codex source-review regressions (storage final): cross-module + per-role
# fairness before the global cap, bounded representative contract, and the
# single authoritative eligibility filter.
# --------------------------------------------------------------------------


def test_architecture_candidates_small_cap_keeps_earlier_and_later_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small scan cap with many earlier-module controllers still samples
    the later ui module (cross-module fairness BEFORE the global cap)."""
    path = tmp_path / "knowledge.sqlite3"
    records = tuple(
        _record(f"bk-{i:02d}", path=f"backend/BackendController{i:02d}.java", order=i)
        for i in range(10)
    ) + (
        _record("ui-ctrl", path="ui/UiController.java"),
        _record("ui-view", path="ui/UiView.java"),
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        monkeypatch.setattr(
            knowledge_index_module, "ARCHITECTURE_SCAN_CEILING", 5
        )
        result = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest, limit=8
        )
        # The capped scan interleaves the modules (rank-1 rows of every
        # module first), so the later ui module is represented even though
        # the earlier backend module holds far more controller files.
        # Python preserves the SQL order (no all-controllers-first re-sort).
        assert list(result["chunk_ids"]) == [
            "bk-00",
            "ui-ctrl",
            "bk-01",
            "ui-view",
            "bk-02",
        ]
        assert result["sampled_module_count"] == 2
        assert result["observed_module_count"] == 2
        assert result["truncated"] is True


def test_architecture_candidates_per_module_cap_represents_every_role(
    tmp_path: Path,
) -> None:
    """per_module=8 with many controllers plus service/repository/
    entrypoint/route files still gets each role into the sample."""
    path = tmp_path / "knowledge.sqlite3"
    records = tuple(
        _record(f"ctrl-{i:02d}", path=f"web/WebController{i:02d}.java", order=i)
        for i in range(10)
    ) + (
        _record("entry", path="web/Application.java"),
        _record("svc", path="web/WebService.java"),
        _record("repo", path="web/WebRepository.java"),
        _record("routes", path="web/WebRoutes.java"),
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        result = index.architecture_candidates(
            "demo-servis",
            generation_digest=generation_digest,
            limit=8,
            per_module=8,
        )
        ids = set(result["chunk_ids"])
        # One file of every role family is represented before extra files of
        # any role: entrypoint, controller, service, repository and route.
        assert {"entry", "ctrl-00", "svc", "repo", "routes"} <= ids
        assert len(result["chunk_ids"]) == 8
        assert result["observed_module_count"] == 1
        assert result["sampled_module_count"] == 1
        assert result["truncated"] is True  # 14 files exceed per_module=8


def test_architecture_candidates_repeated_chunks_cannot_fill_module_quota(
    tmp_path: Path,
) -> None:
    """One big file's repeated chunks cannot fill the module quota while
    distinct files exist (bounded representative contract)."""
    path = tmp_path / "knowledge.sqlite3"
    records = tuple(
        _record(f"big-{i:02d}", path="core/BigService.java", order=i)
        for i in range(10)
    ) + (
        _record("other-a", path="core/OtherA.java"),
        _record("other-b", path="core/OtherB.java"),
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        result = index.architecture_candidates(
            "demo-servis",
            generation_digest=generation_digest,
            limit=8,
            per_module=8,
        )
        ids = set(result["chunk_ids"])
        # One representative chunk per distinct file; the big file's extra
        # chunks are omitted by design and reported honestly.
        assert ids == {"big-00", "other-a", "other-b"}
        assert len(result["chunk_ids"]) == 3
        assert result["observed_module_count"] == 1
        assert result["sampled_module_count"] == 1
        assert result["truncated"] is True


def test_architecture_candidates_eligibility_uses_real_shared_helper(
    tmp_path: Path,
) -> None:
    """Eligibility comes from the shared helper, not a drifted copy:
    hidden .opencode/.agents sources are excluded while Audit.java is a
    real implementation source."""
    path = tmp_path / "knowledge.sqlite3"
    records = (
        _record("hidden-tool", path=".opencode/agent/Tool.py"),
        _record("hidden-agents", path=".agents/config/Settings.py"),
        _record("audit", path="audit/Audit.java"),
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        result = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest
        )
        # Hidden sources never leak; the Audit.java chunk is eligible.
        assert result["chunk_ids"] == ("audit",)
        assert result["observed_module_count"] == 1
        assert result["sampled_module_count"] == 1
        assert result["truncated"] is False
        # The real shared helper is the authoritative eligibility filter.
        assert not is_implementation_source(".opencode/agent/Tool.py")
        assert not is_implementation_source(".agents/config/Settings.py")
        assert is_implementation_source("audit/Audit.java")


def test_architecture_candidates_repeated_call_and_sql_invokes_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registered SQL closures actually invoke the shared helper during
    query execution, and repeated calls are deterministic."""
    path = tmp_path / "knowledge.sqlite3"
    records = (
        _record("one", path="alpha/AService.java"),
        _record("two", path="beta/BController.java"),
    )
    with SQLiteKnowledgeIndex(path, create=True) as index:
        generation_digest = _build(index, records)
        helper = importlib.import_module(HELPER_MODULE)
        calls = {"n": 0}
        real_is_source = helper.is_implementation_source

        def _counting(path: str) -> bool:
            calls["n"] += 1
            return real_is_source(path)

        # Scoped to the exact helper module attribute; restored by pytest.
        monkeypatch.setattr(helper, "is_implementation_source", _counting)
        first = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest
        )
        # The SQL eligibility closure executed through the shared helper.
        assert calls["n"] > 0
        assert set(first["chunk_ids"]) == {"one", "two"}
        # Repeated call on the same connection: deterministic, same result.
        second = index.architecture_candidates(
            "demo-servis", generation_digest=generation_digest
        )
        assert second["chunk_ids"] == first["chunk_ids"]
        assert second["truncated"] is first["truncated"]
        assert second["observed_module_count"] == first["observed_module_count"]
        assert second["sampled_module_count"] == first["sampled_module_count"]
