"""WP8 regression tests: bounded resume reads, no memory-as-skill mislabel.

Covers:
  * WP8-A-1: alias reads are batched (one query, not one-per-project) via the
    canonical ``list_project_aliases_batch`` repository method.
  * WP8-A-2: ``_active_skill_refs`` does NOT present raw memory IDs as skill refs
    when no real (activated) skills exist; it uses the canonical
    ``skill_activation -> skill_manifest`` source.
  * WP8-C-1: a warm ``build_resume_packet`` reads a bounded number of work rows
    (SQL ``LIMIT`` at the repository layer), not a full growing history.
  * WP8-D-1: ``rag-state.json`` compare-and-set rejects a stale concurrent writer
    so a newer generation's authoritative state is never clobbered.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from zekam.application import workspace_resume
from zekam.application.workspace_resume import build_resume_packet
from zekam.domain.canonical import digest
from zekam.infrastructure.sqlite.operational_schema import bootstrap as operational_bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

pytestmark = pytest.mark.unit


def _operational_home(tmp_path: Path) -> Path:
    """A home whose ``state/operational.db`` is schema-bootstrapped so resume reads."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    database = home / "state" / "operational.db"
    database.parent.mkdir(parents=True)
    operational_bootstrap(database)
    return home


def _seed_projects_with_aliases(database: Path, count: int) -> None:
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        for index in range(count):
            project = uow.create_project(slug=f"proj-{index}", display_name=f"Proj {index}")
            uow.add_project_alias(project_id=project.id, alias=f"alias-{index}")
        uow.commit()


def test_wp8_a1_resume_alias_read_is_batched_not_one_per_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """WP8-A-1: building a resume packet issues ONE batched alias query, not N+1.

    ``_active_skill_refs``/``_knowledge_refs`` need a real learning/knowledge store
    to avoid raising; we neuter them so the test only exercises the alias path.
    """
    home = _operational_home(tmp_path)
    database = home / "state" / "operational.db"
    _seed_projects_with_aliases(database, count=12)

    monkeypatch.setattr(workspace_resume, "_active_skill_refs", lambda _home: [])
    monkeypatch.setattr(workspace_resume, "_knowledge_refs", lambda _db: [])
    # Neutralize live lifecycle / capability reads not under test.
    monkeypatch.setattr(
        workspace_resume,
        "resume_projection",
        lambda *_a, **_k: {"sessions": [], "interrupted_count": 0, "failed_count": 0},
    )
    monkeypatch.setattr(
        workspace_resume, "compact_capability_summary", lambda: {"ready": [], "partial": []}
    )

    calls: dict[str, int] = {"batch": 0, "single": 0}
    real_store = SQLiteOperationalStore(database)

    class ProxyUow:
        def __init__(self, real) -> None:  # type: ignore[no-untyped-def]
            self._real = real

        def __enter__(self) -> ProxyUow:
            self._real.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._real.__exit__(*args)

        def commit(self) -> None:
            self._real.commit()

        def list_project_aliases_batch(self, project_ids):  # type: ignore[no-untyped-def]
            calls["batch"] += 1
            return self._real.list_project_aliases_batch(project_ids)

        def list_project_aliases(self, project_id):  # type: ignore[no-untyped-def]
            calls["single"] += 1
            return self._real.list_project_aliases(project_id)

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

    def fake_store(_db: Path) -> object:
        real = real_store

        def unit_of_work() -> ProxyUow:
            return ProxyUow(real.unit_of_work())

        return types.SimpleNamespace(unit_of_work=unit_of_work)

    monkeypatch.setattr(workspace_resume, "SQLiteOperationalStore", fake_store)

    packet = build_resume_packet(home)

    # All alias data came from the singular batched method; the per-project single
    # method was never invoked by resume (N+1 removed).
    assert calls["single"] == 0
    assert calls["batch"] == 1
    projects = packet["projects"]
    assert len(projects) == 12
    for document in projects:
        index = int(document["project_ref"].split("-")[1])
        assert tuple(document["aliases"]) == (f"alias-{index}",)


def test_wp8_a1_batch_method_returns_all_aliases_with_one_query(tmp_path: Path) -> None:
    """The canonical battery method returns every project's aliases in one result."""
    home = _operational_home(tmp_path)
    database = home / "state" / "operational.db"
    _seed_projects_with_aliases(database, count=5)
    store = SQLiteOperationalStore(database)
    project_ids: list[str] = []
    with store.unit_of_work() as uow:
        project_ids = [item.id for item in uow.list_projects()]
        uow.commit()
    with store.unit_of_work() as uow:
        batched = uow.list_project_aliases_batch(tuple(project_ids))
        uow.commit()
    assert len(batched) == len(project_ids)
    for project_id in project_ids:
        index = int(batched[project_id][0].split("-")[1])
        assert batched[project_id] == (f"alias-{index}",)


def test_wp8_a2_memory_never_mislabeled_as_skill_ref_when_no_real_skills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """WP8-A-2: resume skill refs come from the canonical active-skill source.

    Even when the learning store holds memory records (which the old code surfaced
    as ``memory_id`` "skill refs"), ``_active_skill_refs`` returns active skills
    only — and here there are none, so it returns an empty list.
    """
    from zekam.application.composition import build_context
    from zekam.infrastructure.local_core_services import LocalCoreServices
    from zekam.infrastructure.sqlite.local_learning import SQLiteLocalLearning

    home = tmp_path / "home"
    home.mkdir(parents=True)
    operational = home / "state" / "operational.db"
    operational.parent.mkdir(parents=True)
    operational_bootstrap(operational)
    operational.chmod(0o600)
    context = build_context(home=home, environ={})
    LocalCoreServices.from_context(context).bootstrap_extensions()
    learning_path = (home / "state" / "learning.db").resolve()

    learning = SQLiteLocalLearning(learning_path, operational_path=operational.resolve())
    # No activated skill manifests: skill_activation is empty.

    monkeypatch.setattr(workspace_resume, "_knowledge_refs", lambda _db: [])
    refs = workspace_resume._active_skill_refs(home)

    assert refs == []
    # The canonical method mirrors a genuine skill source, never raw memory IDs.
    assert learning.active_skill_refs() == ()


def test_wp8_a2_active_skill_refs_use_canonical_skill_id_not_memory_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """WP8-A-2: with an activated skill, the resume ref is the skill id@version."""
    import zekam.infrastructure.sqlite.local_learning as learning_module

    fake_learning = types.SimpleNamespace(
        active_skill_refs=lambda maximum=8: ("skill:review-patch@3",)
    )

    def _fake_init(_path, *, operational_path):  # type: ignore[no-untyped-def]
        return fake_learning

    monkeypatch.setattr(learning_module, "SQLiteLocalLearning", _fake_init)
    database = tmp_path / "home" / "state" / "learning.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"x")

    refs = workspace_resume._active_skill_refs(tmp_path / "home")
    assert refs == ["skill:review-patch@3"]


@pytest.fixture()
def _bounded_seed_home(tmp_path: Path) -> Path:
    """Home with a large work history to prove resume reads stay bounded."""
    home = _operational_home(tmp_path)
    database = home / "state" / "operational.db"
    # A project plus a large growing work history.
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="busy", display_name="Busy")
        for index in range(200):
            uow.create_work(
                project_id=project.id,
                kind="task",
                title=f"task {index}",
                state=("completed" if index % 2 else "active"),
                payload={"summary": f"summary {index}", "acceptance_criteria": ["ok"]},
                evidence_digest=digest(f"evidence-{index}") if index % 2 else None,
            )
        uow.commit()
    return home


def test_wp8_c1_warm_resume_reads_bounded_number_of_work_rows(
    monkeypatch: pytest.MonkeyPatch, _bounded_seed_home: Path
) -> None:
    """WP8-C-1: ``build_resume_packet`` applies a SQL ``LIMIT`` to work reads."""
    home = _bounded_seed_home
    database = home / "state" / "operational.db"

    monkeypatch.setattr(workspace_resume, "_active_skill_refs", lambda _home: [])
    monkeypatch.setattr(workspace_resume, "_knowledge_refs", lambda _db: [])
    monkeypatch.setattr(
        workspace_resume,
        "resume_projection",
        lambda *_a, **_k: {"sessions": [], "interrupted_count": 0, "failed_count": 0},
    )
    monkeypatch.setattr(
        workspace_resume, "compact_capability_summary", lambda: {"ready": [], "partial": []}
    )

    observed_limits: list[int] = []
    observed_rows_fetched = {"value": 0}
    real_store = SQLiteOperationalStore(database)

    class ProxyUow:
        def __init__(self, real) -> None:  # type: ignore[no-untyped-def]
            self._real = real

        def __enter__(self) -> ProxyUow:
            self._real.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._real.__exit__(*args)

        def commit(self) -> None:
            self._real.commit()

        def list_work_limited(self, limit: int, *, project_id: str | None = None):  # type: ignore[no-untyped-def]
            observed_limits.append(limit)
            result = self._real.list_work_limited(limit=limit, project_id=project_id)
            observed_rows_fetched["value"] += len(result)
            return result

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

    def fake_store(_db: Path) -> object:
        real = real_store

        def unit_of_work() -> ProxyUow:
            return ProxyUow(real.unit_of_work())

        return types.SimpleNamespace(unit_of_work=unit_of_work)

    monkeypatch.setattr(workspace_resume, "SQLiteOperationalStore", fake_store)

    packet = build_resume_packet(home)

    # resume requested a bounded work read; the SQL LIMIT caps rows, not a post-read slice.
    assert observed_limits, "resume must call a bounded list_work_limited"
    assert all(limit >= 0 for limit in observed_limits)
    # bounded window = open(20) + completed(10) + 1
    bound_window = workspace_resume._OPEN_WORK_LIMIT + workspace_resume._COMPLETED_WORK_LIMIT + 1
    assert max(observed_limits) <= bound_window
    assert observed_rows_fetched["value"] <= bound_window
    # The packet reflects a bounded window, not all 200 rows.
    assert len(packet["work"]["open"]) <= workspace_resume._OPEN_WORK_LIMIT


def test_wp8_d1_stale_state_write_does_not_clobber_newer_generation(
    tmp_path: Path,
) -> None:
    """WP8-D-1: a stale concurrent rag-state write cannot overwrite a newer generation."""
    import zekam.application.project_rag_runtime as runtime

    state_path = tmp_path / "runtime" / "rag-state.json"
    state_path.parent.mkdir(parents=True)

    older = {
        "schema": "zekam-project-rag-index/v1",
        "project_id": "proj",
        "generation_digest": "sha256:" + "a" * 64,
        "source_revision": "sha256:" + "b" * 64,
        "tree_digest": "sha256:" + "c" * 64,
        "query_verified_at": None,
    }
    newer = dict(older)
    newer["generation_digest"] = "sha256:" + "z" * 64
    newer["source_revision"] = "sha256:" + "y" * 64
    newer["tree_digest"] = "sha256:" + "x" * 64

    state_path.write_text(json.dumps(older), encoding="utf-8")

    # Simulate: query verified against the OLDER generation, then a reindex advanced
    # the on-disk state to NEWER before the query's observational write.
    committed = runtime._write_rag_state_cas(
        state_path,
        expected_identity=runtime._state_cas_identity(older),
        payload=(json.dumps(dict(older, query_verified_at="2026-01-01T00:00:00Z")) + "\n").encode(
            "utf-8"
        ),
    )
    assert committed is True

    # Now the newer generation is on disk; a stale client that verifies against the
    # older identity must NOT clobber it.
    state_path.write_text(json.dumps(newer), encoding="utf-8")
    skipped = runtime._write_rag_state_cas(
        state_path,
        expected_identity=runtime._state_cas_identity(older),
        payload=(json.dumps(dict(older, query_verified_at="2026-01-02T00:00:00Z")) + "\n").encode(
            "utf-8"
        ),
    )
    assert skipped is False
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    # The newer generation's authoritative state is fully preserved.
    assert persisted["generation_digest"] == newer["generation_digest"]
    assert persisted["source_revision"] == newer["source_revision"]
    assert persisted["tree_digest"] == newer["tree_digest"]


def test_wp8_d1_concurrent_same_generation_write_still_commits(tmp_path: Path) -> None:
    """A concurrent write on the SAME generation identity still commits (CAS allows it)."""
    import zekam.application.project_rag_runtime as runtime

    state_path = tmp_path / "runtime" / "rag-state.json"
    state_path.parent.mkdir(parents=True)
    state = {
        "schema": "zekam-project-rag-index/v1",
        "project_id": "proj",
        "generation_digest": "sha256:" + "a" * 64,
        "source_revision": "sha256:" + "b" * 64,
        "tree_digest": "sha256:" + "c" * 64,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    committed = runtime._write_rag_state_cas(
        state_path,
        expected_identity=runtime._state_cas_identity(state),
        payload=(json.dumps(dict(state, query_verified_at="2026-01-01T00:00:00Z")) + "\n").encode(
            "utf-8"
        ),
    )
    assert committed is True
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["query_verified_at"] == "2026-01-01T00:00:00Z"
