"""Native yardimci olcum testleri icin gercek Maven fixture + SQLite ledger destegi."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from zekam.application.composition import build_context
from zekam.application.unit_test_runtime import (
    MavenMeasurementBinding,
    NativeMeasurement,
    compose_native_measurement,
    measure_native,
    native_request,
)
from zekam.domain.unit_test_engineering import (
    UnitTestBudget,
    UnitTestRequest,
)
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.infrastructure.unit_test_runner.maven_plan import build_unit_test_plan

FIXTURE = Path(__file__).parents[1] / "fixtures" / "unit_test_engineering_java"
SOURCE = "src/main/java/com/zekam/fixture/Calculator.java"
EXTRA_TEST = "src/test/java/com/zekam/fixture/CalculatorBranchTest.java"
EXTRA_TEST_BODY = """package com.zekam.fixture;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalculatorBranchTest {
    @Test
    void classifiesNegative() {
        assertEquals("negative", Calculator.classify(-4));
    }

    @Test
    void classifiesPositive() {
        assertEquals("positive", Calculator.classify(9));
    }
}
"""


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@dataclass
class Env:
    root: Path
    home: Path
    database: Path
    project_id: str
    revision: str

    def request(
        self,
        *,
        percent: str = "50",
        max_attempts: int = 3,
        timeout: int = 180,
        source: str = SOURCE,
        revision: str | None = None,
    ) -> UnitTestRequest:
        return native_request(
            UnitTestRequest.with_defaults(
                project_id=self.project_id,
                source_binding_id="binding-1",
                source_revision=revision or self.revision,
                source_files=[source],
                percent=percent,
                budget=UnitTestBudget(max_attempts, timeout, max(timeout, 300)),
            )
        )

    def measure(
        self,
        request: UnitTestRequest,
        *,
        wrap: Callable[[object], object] | None = None,
        root: Path | None = None,
    ) -> NativeMeasurement:
        project_root = root or self.root
        planned = build_unit_test_plan(project_root, target_modules=("",))
        assert planned.plan is not None, planned.reasons
        binding = MavenMeasurementBinding(
            project_root=project_root,
            object_store_root=self.home / "artifacts" / "sha256",
            lock_dir=self.home / "runtime" / "unit-test-locks",
            approved_maven_plan_digest=planned.plan.plan_digest,
            target_modules=("",),
            allow_network=True,
        )
        store = SQLiteOperationalStore(self.database)
        ports = compose_native_measurement(request, store=store, binding=binding)
        if wrap is not None:
            ports = dataclasses.replace(ports, measurer=wrap(ports.measurer))
        result = measure_native(request, ports)
        return result

    def ledger_state(self, request: UnitTestRequest):
        store = SQLiteOperationalStore(self.database)
        with store.unit_of_work() as uow:
            ledger = uow.unit_test_ledger()
            attempts = ledger.list_attempts(request.request_digest)
            receipts = {a.attempt_id: ledger.get_attempt_receipt(a.attempt_id) for a in attempts}
            observations = {a.attempt_id: ledger.list_observations(a.attempt_id) for a in attempts}
            terminals = ledger.list_terminals(request.request_digest)
            uow.commit()
        return attempts, receipts, observations, terminals

    def evidence(self, digest_value: str) -> dict:
        objects = LocalContentAddressedStore(self.home / "artifacts" / "sha256")
        return json.loads(objects.get(digest_value).decode("utf-8"))


def make_env(tmp_path: Path) -> Env:
    root = tmp_path / "proje"
    shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("target"))
    (root / ".gitignore").write_text("target/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    revision = _git(root, "rev-parse", "HEAD")
    home = tmp_path / "home"
    context = build_context(home=home)
    database = context.settings.database.sqlite_path(context.home)
    assert schema.bootstrap_v6(database).schema_ok
    store = SQLiteOperationalStore(database)
    with store.unit_of_work() as uow:
        project = uow.create_project(slug="fixture", display_name="Fixture")
        uow.commit()
    return Env(root, context.home, database, project.id, revision)


@dataclass
class Bindings:
    realm_id: str
    work_item_id: str
    source_binding_id: str
    source_snapshot_id: str
    graph_generation_digest: str


def make_bindings(env: Env, tmp_path: Path) -> Bindings:
    """Exact operational + graph baglari: project/realm/work/source binding/snapshot/graph."""

    import sqlite3
    from uuid import uuid4

    from zekam.application.code_graph import (
        apply_graph_build,
        graph_store_path,
        plan_graph_build,
    )
    from zekam.application.code_graph_python import PythonAstExtractor
    from zekam.domain.canonical import digest
    from zekam.infrastructure.sqlite.code_graph import SQLiteCodeGraphStore

    store = SQLiteOperationalStore(env.database)
    with store.unit_of_work() as uow:
        source = uow.bind_source(
            project_id=env.project_id, portable_ref="project/fixture", source_kind="git"
        )
        snapshot = uow.capture_source_snapshot(
            source_binding_id=source.id,
            revision_ref=env.revision,
            tree_digest=digest("tree"),
            content_digest=digest("content"),
            config_digest=digest("config"),
        )
        work = uow.create_work(
            project_id=env.project_id,
            kind="task",
            title="Native test olcumu",
            state="active",
            payload_digest=digest("work"),
        )
        uow.commit()
    realm = str(uuid4())
    with sqlite3.connect(env.database) as connection:
        connection.execute(
            "insert into project_knowledge_realm values(?,?,?)",
            (env.project_id, realm, "2026-10-05T00:00:00+00:00"),
        )
    graph_root = tmp_path / "graph-source"
    graph_root.mkdir()
    (graph_root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    graph_path = graph_store_path(env.home, "fixture")
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph = SQLiteCodeGraphStore(graph_path, create=True)
    extractor = PythonAstExtractor()
    plan = plan_graph_build(
        graph_root,
        project_id=env.project_id,
        project_slug="fixture",
        source_revision=env.revision,
        extractor=extractor,
        created_at="2026-10-05T00:00:00Z",
    )
    generation = apply_graph_build(graph, extractor, graph_root, plan).generation_digest
    graph.close()
    return Bindings(realm, work.id, source.id, snapshot.id, generation)
