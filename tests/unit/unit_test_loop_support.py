"""W05 test destegi: REPLAY/MOCK agent, olcum ve ortam; GERCEK ledger, CAS ve dosya sistemi.

Gercek olanlar: SQLite operational v6 ledger, yerel icerik adresli nesne deposu, dosya sistemi
patch calisma alani, ``CanonicalUnitTestAgentGateway`` + ``CanonicalAgentDispatchService``
(assignment-first). Taklit (replay/mock) olanlar: istemci adapter'i (``ReplayClientAdapter``),
Maven olcumu (``ScriptedMeasurer``: kayitli test agacindan sentetik ama gercek
``MeasurementRecord`` uretir) ve ortam gozlemcisi. Gercek model/Maven davranisi kanitlamaz.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from zekam.application.unit_test_agents import (
    AgentSpecialty,
    CanonicalUnitTestAgentGateway,
    WorkBinding,
)
from zekam.application.unit_test_loop import UnitTestLoopService
from zekam.application.unit_test_loop_state import (
    EnvironmentObservation,
    InMemoryLoopControl,
    LoopOutcome,
    MeasuredRun,
)
from zekam.application.unit_test_measurement import (
    FreshnessVerdict,
    MeasurementBinding,
    MeasurementEvidence,
    MeasurementScope,
    RunStatus,
    TestRunKind,
    ToolchainIdentity,
    assemble_measurement,
)
from zekam.application.unit_test_plateau import LoopLimits, UnitTestApprovals
from zekam.domain.agents import AgentAssignment, AgentInvocation
from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes
from zekam.domain.clients import (
    CanonicalDispatchPermit,
    ClientCapabilityManifest,
    ClientDescriptor,
    ClientKind,
    ClientLifecycleEvent,
    DispatchOutcome,
    DispatchRequest,
    DispatchResult,
)
from zekam.domain.unit_test_engineering import (
    CoverageMetric,
    CoverageObservation,
    UnitTestBudget,
    UnitTestRequest,
)
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore
from zekam.infrastructure.sqlite.unit_test_ledger import SQLiteUnitTestLedger
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.infrastructure.unit_test_runner.patch_workspace import FileSystemTestPatchWorkspace

SRC = "src/main/java/p/A.java"
TEST_DIR = "src/test/java/p"
CONFIG = digest("build-config")
LAUNCHER = digest("launcher")
TOTAL_LINES = 10
BASE_COVERED = 3

Handler = Callable[[Mapping[str, Any], int], Mapping[str, Any]]


def scenario_doc(
    sid: str,
    *,
    oracle: str = "explicit-contract",
    priority: str = "high",
    ref: str = "spec#1",
    material: bool | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "scenario_id": sid,
        "risk_id": f"R-{sid}",
        "behavior_contract": f"A davranisi {sid}",
        "source_files": [SRC],
        "oracle_kind": oracle,
        "oracle_ref": "" if oracle == "unresolved-assumption" else ref,
        "oracle_confidence": "medium",
        "priority": priority,
        "method": "example",
        "acceptance": "beklenen sonuc gozlenir",
    }
    if material is not None:
        doc["material"] = material
    return doc


def default_plan() -> dict[str, Any]:
    return {
        "scenarios": [
            scenario_doc("S1"),
            scenario_doc("S2", oracle="invariant", priority="normal"),
            scenario_doc("S3", oracle="unresolved-assumption", priority="normal"),
        ]
    }


def make_test_file(sid: str, cover: int = 3, extra: str = "") -> dict[str, Any]:
    test = f"@Test void t() {{ assertEquals(1, f({cover})); }}"
    body = f"// COVER:{cover}\n{extra}class {sid}Test {{ {test} }}\n"
    return {"path": f"{TEST_DIR}/{sid}Test.java", "content": body, "preimage_digest": None}


def builder_files(
    context: Mapping[str, Any],
    *,
    cover: int = 3,
    extra: str = "",
) -> list[dict[str, Any]]:
    return [make_test_file(s["scenario_id"], cover, extra) for s in context["scenarios"]]


def default_builder(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
    del call
    owned = dict(context.get("owned_files", []))
    files = builder_files(context)
    for item in files:
        item["preimage_digest"] = owned.get(item["path"])
    return {
        "patch": {"files": files},
        "links": [
            {"scenario_id": s["scenario_id"], "test_ref": f"p.{s['scenario_id']}Test#t"}
            for s in context["scenarios"]
        ],
        "approach": {
            "strategy": context.get("required_strategy") or "alternate-fixture",
            "note": "replay",
        },
    }


def default_planner(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
    del context, call
    return {"plan": default_plan(), "context_gaps": []}


def default_verifier(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
    del call
    return {
        "verdict": "accepted",
        "scenario_results": [
            {"scenario_id": s["scenario_id"], "status": "verified"} for s in context["scenarios"]
        ],
        "quality_findings": [],
        "new_material_risks": [],
    }


def default_final(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
    del context, call
    return {"verdict": "accepted", "scenario_results": [], "new_material_risks": []}


def default_triage(context: Mapping[str, Any], call: int) -> Mapping[str, Any]:
    del call
    sid = context["scenarios"][0]["scenario_id"]
    return {
        "verdict": "rejected",
        "defect_review": {
            "scenario_id": sid,
            "finding": "defect-confirmed",
            "evidence_refs": ["spec#1"],
        },
    }


DEFAULT_AGENT_IDS: dict[AgentSpecialty, str] = {
    AgentSpecialty.ANALYZER_PLANNER: "planner-1",
    AgentSpecialty.BUILDER: "builder-1",
    AgentSpecialty.VERIFIER: "verifier-1",
    AgentSpecialty.DEFECT_TRIAGE: "triage-1",
    AgentSpecialty.FINAL_VERIFIER: "final-verifier-1",
}


class ReplayClientAdapter:
    """REPLAY/MOCK istemci: context'i nesne deposundan digest ile okur, kayitli govdeyi doner."""

    def __init__(
        self,
        specialty: AgentSpecialty,
        objects: LocalContentAddressedStore,
        handler: Handler,
        *,
        agent_id: str,
        events: list[str],
        outcome: DispatchOutcome = DispatchOutcome.SUCCESS,
        extra_result: Mapping[str, Any] | None = None,
        artifact: bool = False,
    ) -> None:
        self.artifact = artifact
        self.last_payload: dict[str, Any] = {}
        self.specialty = specialty
        self.objects = objects
        self.handler = handler
        self.agent_id = agent_id
        self.events = events
        self.outcome = outcome
        self.calls = 0
        self.contexts: list[Mapping[str, Any]] = []
        self.extra_result = dict(extra_result or {})
        self._descriptor = ClientDescriptor(
            ClientKind.INTERNAL,
            f"replay-mock-{specialty.value}",
            "replay-agent",
            frozenset({"structured-result"}),
        )

    @property
    def descriptor(self) -> ClientDescriptor:
        return self._descriptor

    @property
    def capability_manifest(self) -> ClientCapabilityManifest:
        return self._descriptor.capability_manifest

    def lifecycle_event(
        self,
        *,
        session_id: str,
        sequence: int,
        previous_digest: str | None,
        event_type: str,
        payload_digest: str,
        occurred_at: dt.datetime,
    ) -> ClientLifecycleEvent:
        raise NotImplementedError

    def dispatch(
        self, request: DispatchRequest, *, cwd: Path, permit: CanonicalDispatchPermit
    ) -> DispatchResult:
        permit.assert_valid(request)
        self.events.append(f"adapter:{self.specialty.value}")
        self.calls += 1
        document = json.loads(self.objects.get(request.context_manifest_digest).decode("utf-8"))
        context = document["context"]
        self.contexts.append(context)
        if self.outcome is not DispatchOutcome.SUCCESS:
            return DispatchResult(
                request.assignment_id,
                request.invocation_id,
                request.client_id,
                request.role,
                self.outcome,
                1,
                {},
                failure_category="replay-failure",
            )
        body = dict(self.handler(context, self.calls))
        result = {
            "agent_id": self.agent_id,
            "status": "completed",
            "summary": f"replay {self.specialty.value}",
            "evidence": [{"kind": "replay", "reference": f"call-{self.calls}"}],
        } | self.extra_result
        payload: dict[str, Any] = {"result": result, "body": body}
        if self.artifact:
            stored = self.objects.put(canonical_bytes(payload), media_type="application/json")
            payload = {"artifact_digest": stored.digest}
        self.last_payload = payload
        return DispatchResult(
            request.assignment_id,
            request.invocation_id,
            request.client_id,
            request.role,
            DispatchOutcome.SUCCESS,
            0,
            payload,
        )


class RecordingStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.assignments: list[AgentAssignment] = []
        self.invocations: list[AgentInvocation] = []
        self.results: list[str] = []

    def create(self, assignment: AgentAssignment) -> tuple[UUID, bool]:
        self.events.append(f"store:assignment:{assignment.agent_ref}")
        self.assignments.append(assignment)
        return assignment.id, True

    def record_invocation(self, invocation: AgentInvocation) -> tuple[UUID, bool]:
        self.events.append("store:invocation")
        self.invocations.append(invocation)
        return invocation.id, True

    def store_result(
        self, *, assignment_id: UUID, invocation_id: UUID, envelope_digest: str
    ) -> None:
        self.events.append("store:result")
        self.results.append(envelope_digest)


def build_record(
    request: UnitTestRequest,
    *,
    candidate: str,
    attempt_id: str,
    run_id: str,
    production_digest: str,
    covered: int,
    kind: TestRunKind = TestRunKind.PASSED,
    run_status: RunStatus = RunStatus.COMPLETED,
    exit_code: int = 0,
    tests: int = 2,
    toolchain: str = LAUNCHER,
) -> Any:
    binding = MeasurementBinding(
        request_digest=request.request_digest,
        attempt_id=attempt_id,
        run_id=run_id,
        scope=MeasurementScope.UNIT,
        source_revision=request.source_revision,
        production_digests=((SRC, production_digest),),
        class_digests=(),
        test_candidate_digest=candidate,
        build_config_digest=CONFIG,
        plan_digest=digest("maven-plan"),
        toolchain=ToolchainIdentity("wrapper", toolchain, "3.9.9", "21.0.1", "17", ()),
        report_digests=(("surefire:r", digest("report")),),
    )
    observation = CoverageObservation.measured(
        SRC, CoverageMetric.LINE, covered=covered, missed=TOTAL_LINES - covered
    )
    counts = (
        ("discovered", tests),
        ("executed", tests),
        ("passed", tests if kind is TestRunKind.PASSED else 0),
        ("failed", 0 if kind is TestRunKind.PASSED else 1),
        ("errors", 0),
        ("skipped", 0),
        ("aborted", 0),
    )
    evidence = MeasurementEvidence(
        run_status,
        exit_code,
        kind,
        counts,
        (observation,) if run_status is RunStatus.COMPLETED else (),
    )
    return assemble_measurement(
        request,
        binding if run_status is RunStatus.COMPLETED else None,
        evidence,
        freshness=FreshnessVerdict.FRESH,
    )


_COVER = re.compile(r"// COVER:(\d+)")
_FAIL = re.compile(r"// FAIL:(\w+)")


class ScriptedMeasurer:
    """MOCK olcum: gercek test agacini okur; // COVER:n, // FAIL:kind, // FLAKY isaretleri."""

    def __init__(self, root: Path, *, on_measure: Callable[[int], None] | None = None) -> None:
        self.root = root
        self.on_measure = on_measure
        self.runs: list[dict[str, Any]] = []
        self.raise_on: set[int] = set()
        self.status_for: dict[int, RunStatus] = {}
        self._flaky_seen: dict[str, int] = {}

    def measure(
        self,
        request: UnitTestRequest,
        *,
        attempt_id: str,
        run_id: str,
        candidate_digest: str,
        timeout_seconds: float,
        cancel: threading.Event,
    ) -> MeasuredRun:
        number = len(self.runs) + 1
        self.runs.append(
            {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "candidate": candidate_digest,
                "timeout": timeout_seconds,
            }
        )
        if number in self.raise_on:
            raise RuntimeError("simulated crash")
        if self.on_measure is not None:
            self.on_measure(number)
        production = digest_of_bytes((self.root / SRC).read_bytes())
        forced = self.status_for.get(number)
        if forced is not None:
            return MeasuredRun(
                build_record(
                    request,
                    candidate=candidate_digest,
                    attempt_id=attempt_id,
                    run_id=run_id,
                    production_digest=production,
                    covered=0,
                    run_status=forced,
                )
            )
        if cancel.is_set():
            record = build_record(
                request,
                candidate=candidate_digest,
                attempt_id=attempt_id,
                run_id=run_id,
                production_digest=production,
                covered=0,
                run_status=RunStatus.CANCELLED,
            )
            return MeasuredRun(record)
        covered = 0
        failing: list[str] = []
        fail_kind = ""
        flaky = False
        for path in sorted((self.root / "src" / "test").rglob("*.java")):
            text = path.read_text(encoding="utf-8")
            covered += sum(int(m) for m in _COVER.findall(text))
            failure = _FAIL.search(text)
            if failure is not None:
                fail_kind = failure.group(1)
                failing.append(f"p.{path.stem}#t")
            if "// FLAKY" in text:
                flaky = True
                seen = self._flaky_seen.get(candidate_digest, 0)
                self._flaky_seen[candidate_digest] = seen + 1
                if seen == 0:
                    fail_kind = "assertion"
                    failing.append(f"p.{path.stem}#t")
        covered = min(TOTAL_LINES, covered)
        if not failing:
            record = build_record(
                request,
                candidate=candidate_digest,
                attempt_id=attempt_id,
                run_id=run_id,
                production_digest=production,
                covered=covered,
            )
            return MeasuredRun(record)
        tails = {
            "assertion": "AssertionFailedError: expected: <1> but was: <2>",
            "compile": "COMPILATION ERROR cannot find symbol",
            "setup": "ExceptionInInitializerError in @BeforeEach setUp(",
            "env": "Could not resolve dependencies for project",
            "unknown": "something odd happened",
        }
        compile_error = fail_kind == "compile"
        record = build_record(
            request,
            candidate=candidate_digest,
            attempt_id=attempt_id,
            run_id=run_id,
            production_digest=production,
            covered=covered,
            kind=TestRunKind.UNAVAILABLE if compile_error else TestRunKind.FAILED,
            exit_code=1,
        )
        del flaky
        return MeasuredRun(
            record,
            failed_cases=() if compile_error else tuple(failing),
            output_tail=tails.get(fail_kind, ""),
            output_bytes=len(tails.get(fail_kind, "")),
        )


@dataclass(slots=True)
class FakeEnvironment:
    root: Path
    revision: str
    config: str = CONFIG
    launcher: str = LAUNCHER
    dirty: frozenset[str] = frozenset()
    revision_override: str | None = None
    calls: int = 0

    def observe(self) -> EnvironmentObservation:
        self.calls += 1
        return EnvironmentObservation(
            source_revision=self.revision_override or self.revision,
            production_digests={SRC: digest_of_bytes((self.root / SRC).read_bytes())},
            build_config_digest=self.config,
            launcher_digest=self.launcher,
            protected_dirty_paths=self.dirty,
            test_tree_digests={
                p.relative_to(self.root).as_posix(): digest_of_bytes(p.read_bytes())
                for p in sorted((self.root / "src" / "test").rglob("*.java"))
            },
        )


@dataclass
class Harness:
    tmp: Path
    percent: str = "80"
    max_attempts: int = 10
    limits: LoopLimits = field(
        default_factory=lambda: LoopLimits(batch_size=3, max_remote_calls=1000)
    )
    remote: bool = False
    handlers: dict[AgentSpecialty, Handler] = field(default_factory=dict)
    agent_ids: dict[AgentSpecialty, str] = field(default_factory=dict)
    outcomes: dict[AgentSpecialty, DispatchOutcome] = field(default_factory=dict)
    extra_result: dict[str, Any] = field(default_factory=dict)
    process_timeout: int = 60
    artifact_bodies: bool = True

    def __post_init__(self) -> None:
        self.root = self.tmp / "proj"
        (self.root / "src/main/java/p").mkdir(parents=True)
        (self.root / SRC).write_text("class A {}\n", encoding="utf-8")
        (self.root / TEST_DIR).mkdir(parents=True)
        (self.root / TEST_DIR / "ExistingTest.java").write_text(
            f"// COVER:{BASE_COVERED}\nclass ExistingTest "
            "{ @Test void t() { assertEquals(1, 1); } }\n",
            encoding="utf-8",
        )
        db_path = self.tmp / "operational.sqlite"
        schema.bootstrap_v6(db_path)
        store = SQLiteOperationalStore(db_path)
        with store.unit_of_work() as uow:
            project = uow.create_project(slug="demo", display_name="Demo")
            uow.commit()
        import sqlite3

        self.connection = sqlite3.connect(db_path, isolation_level=None)
        self.connection.execute("pragma foreign_keys=on")
        self.ledger = SQLiteUnitTestLedger(self.connection)
        self.objects = LocalContentAddressedStore(self.tmp / "cas").ensure()
        self.workspace = FileSystemTestPatchWorkspace(self.root)
        self.request = UnitTestRequest.with_defaults(
            project_id=project.id,
            source_binding_id="bind1",
            source_revision="rev-1",
            source_files=[SRC],
            percent=self.percent,
            budget=UnitTestBudget(self.max_attempts, self.process_timeout, 3600),
            allowed_test_paths=["src/test/java"],
        )
        self.environment = FakeEnvironment(self.root, "rev-1")
        self.measurer = ScriptedMeasurer(self.root)
        self.control = InMemoryLoopControl()
        self.events: list[str] = []
        self.store = RecordingStore(self.events)
        self.work = WorkBinding(uuid4(), uuid4(), uuid4(), uuid4())
        self.approvals = UnitTestApprovals(
            write_tests=True,
            build=True,
            remote_model=self.remote,
            approved_budget=self.request.budget,
            approved_limits=self.limits,
        )
        self.adapters = self._make_adapters()
        self.gateway = self._make_gateway()

    def _make_adapters(self) -> dict[AgentSpecialty, ReplayClientAdapter]:
        defaults: dict[AgentSpecialty, Handler] = {
            AgentSpecialty.ANALYZER_PLANNER: default_planner,
            AgentSpecialty.BUILDER: default_builder,
            AgentSpecialty.VERIFIER: default_verifier,
            AgentSpecialty.DEFECT_TRIAGE: default_triage,
            AgentSpecialty.FINAL_VERIFIER: default_final,
        }
        return {
            spec: ReplayClientAdapter(
                spec,
                self.objects,
                self.handlers.get(spec, handler),
                agent_id=self.agent_ids.get(spec, DEFAULT_AGENT_IDS[spec]),
                events=self.events,
                outcome=self.outcomes.get(spec, DispatchOutcome.SUCCESS),
                extra_result=self.extra_result,
                artifact=self.artifact_bodies,
            )
            for spec, handler in defaults.items()
        }

    def _make_gateway(self) -> CanonicalUnitTestAgentGateway:
        return CanonicalUnitTestAgentGateway(
            work=self.work,
            store=self.store,
            objects=self.objects,
            adapters=self.adapters,
            cwd=self.root,
            is_remote=self.remote,
        )

    def service(self) -> UnitTestLoopService:
        return UnitTestLoopService(
            ledger=self.ledger,
            objects=self.objects,
            gateway=self.gateway,
            measurer=self.measurer,
            workspace=self.workspace,
            environment=self.environment,
            control=self.control,
        )

    def run(self, **overrides: Any) -> LoopOutcome:
        return self.service().run(
            self.request,
            approvals=overrides.pop("approvals", self.approvals),
            limits=overrides.pop("limits", self.limits),
        )

    def calls(self, spec: AgentSpecialty) -> int:
        return self.adapters[spec].calls

    def tree(self) -> set[str]:
        return {
            p.relative_to(self.root).as_posix()
            for p in (self.root / "src" / "test").rglob("*.java")
        }
