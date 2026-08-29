"""PostgreSQL-backed read-only projection for the Neuro Observatory."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.application.config import DatabaseSettings
from zekam.application.observatory import (
    CanonicalRuntimeEntity,
    CanonicalRuntimeProjection,
    ObservatoryAgent,
    ObservatoryEvent,
    RuntimeContradiction,
    RuntimeProjection,
    runtime_projection_digest,
    sanitize_observatory_label,
)
from zekam.domain.canonical import digest
from zekam.domain.observability import (
    CausalEdge,
    CausalNode,
    CausalOrphan,
    CausalProjection,
    GraphEdge,
    GraphNode,
    ProjectionTile,
)
from zekam.infrastructure.postgres.connection import session

_WORK_LIMIT = 48
_JOB_LIMIT = 40
_MODEL_LIMIT = 24
_KNOWLEDGE_LIMIT = 24
_MEMORY_LIMIT = 24
_SCHEDULER_LIMIT = 16
_EVENT_LIMIT = 80
_AGENT_LIMIT = 32
_CAUSAL_NODE_LIMIT = 256
_CAUSAL_EDGE_LIMIT = 512
_ORPHAN_LIMIT = 128
_RUNTIME_ENTITY_LIMIT = 256
_CONTRADICTION_LIMIT = 64


@dataclass(frozen=True, slots=True)
class PostgresObservatoryProjectionReader:
    """Read bounded, realm-scoped UI projections through the application role."""

    settings: DatabaseSettings
    realm_id: UUID

    def read(self) -> RuntimeProjection:
        generated_at = dt.datetime.now(dt.UTC)
        with (
            session(self.settings, realm_id=self.realm_id) as connection,
            connection.transaction(),
        ):
            with connection.cursor() as cursor:
                cursor.execute("set transaction isolation level repeatable read, read only")
            tiles = self._tiles(connection)
            nodes, edges = self._graph(connection)
            agents = self._agents(connection)
            events = self._events(connection)
            causal = self._causal(connection)
            canonical_runtime = self._canonical_runtime(connection)

        material = {
            "realm_id": str(self.realm_id),
            "tiles": [item.as_dict() for item in tiles],
            "nodes": [item.as_dict() for item in nodes],
            "edges": [item.as_dict() for item in edges],
            "agents": [item.as_dict() for item in agents],
            "events": [item.as_dict() for item in events],
            "causal": causal.as_dict(),
            "canonical_runtime": canonical_runtime.as_dict(),
        }
        return RuntimeProjection(
            generated_at=generated_at,
            tiles=tiles,
            nodes=nodes,
            edges=edges,
            agents=agents,
            events=events,
            causal=causal,
            canonical_runtime=canonical_runtime,
            source_digest=runtime_projection_digest(material),
            available=True,
            detail="postgresql-realm-projection",
        )

    def _tiles(self, connection: Any) -> tuple[ProjectionTile, ...]:
        queries = (
            (
                "work",
                "Aktif İşler",
                "select count(*) from work.work_item "
                "where state in ('ready', 'active', 'blocked', 'verification')",
                "db:work.work_item?state=open",
            ),
            (
                "run",
                "Çalışan Run",
                "select count(*) from runtime.job "
                "where state in ('ready', 'running', 'recovery-required')",
                "db:runtime.job?state=active",
            ),
            (
                "model",
                "Modeller",
                "select count(*) from models.model_inventory where enabled = true",
                "db:models.model_inventory?enabled=true",
            ),
            (
                "knowledge",
                "Bilgi Kaynakları",
                "select count(*) from knowledge.source_version where state = 'active'",
                "db:knowledge.source_version?state=active",
            ),
            (
                "memory",
                "Aktif Bellek",
                "select count(*) from memory.record where state = 'active'",
                "db:memory.record?state=active",
            ),
            (
                "scheduler",
                "Zamanlayıcı",
                "select count(*) from ops.job_definition where state = 'active'",
                "db:ops.job_definition?state=active",
            ),
        )
        tiles: list[ProjectionTile] = []
        with connection.cursor() as cursor:
            for key, title, query, drill_down in queries:
                cursor.execute(query)
                row = cursor.fetchone()
                tiles.append(
                    ProjectionTile(
                        key=key,
                        title=title,
                        value=int(row[0]) if row is not None else 0,
                        drill_down=drill_down,
                        detail="realm-scoped canonical projection",
                    )
                )
        return tuple(tiles)

    def _graph(self, connection: Any) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...]]:
        root_id = f"runtime:realm:{str(self.realm_id)[:8]}"
        nodes: list[GraphNode] = [
            GraphNode(
                node_id=root_id,
                kind="runtime-root",
                label="Canlı Runtime",
                canonical_ref=f"db:core.realm/{self.realm_id}",
            )
        ]
        edges: list[GraphEdge] = []
        clusters = {
            "work": ("İş Grafı", "db:work.work_item"),
            "run": ("Run / Ajanlar", "db:runtime.job"),
            "model": ("Model Ağı", "db:models.model_inventory"),
            "knowledge": ("Bilgi Ağı", "db:knowledge.source_version"),
            "memory": ("Bellek", "db:memory.record"),
            "scheduler": ("Zamanlayıcı", "db:ops.job_definition"),
        }
        for key, (label, canonical_ref) in clusters.items():
            cluster_id = f"runtime:cluster:{key}"
            nodes.append(GraphNode(cluster_id, "runtime-cluster", label, canonical_ref))
            edges.append(GraphEdge(root_id, cluster_id, "runtime-domain"))

        work_ids = self._work_nodes(connection, nodes, edges)
        self._work_relations(connection, work_ids, edges)
        self._job_nodes(connection, work_ids, nodes, edges)
        self._model_nodes(connection, nodes, edges)
        self._knowledge_nodes(connection, nodes, edges)
        self._memory_nodes(connection, nodes, edges)
        self._scheduler_nodes(connection, nodes, edges)
        self._lease_nodes(connection, nodes, edges)
        return _unique_nodes(nodes), _unique_edges(edges)

    def _work_nodes(
        self,
        connection: Any,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> set[str]:
        with connection.cursor() as cursor:
            cursor.execute(
                "select id, external_number, type, state, title "
                "from work.work_item order by updated_at desc limit %s",
                (_WORK_LIMIT,),
            )
            rows = cursor.fetchall()
        work_ids: set[str] = set()
        for row in rows:
            work_id = str(row[0])
            work_ids.add(work_id)
            node_id = f"work:{work_id}"
            reference = str(row[1] or work_id[:8])
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    kind="work",
                    label=_bounded_label(f"{reference} · {row[4]}", 148),
                    canonical_ref=f"db:work.work_item/{work_id}",
                )
            )
            edges.append(GraphEdge("runtime:cluster:work", node_id, f"state:{row[3]}"))
        return work_ids

    def _work_relations(
        self,
        connection: Any,
        work_ids: set[str],
        edges: list[GraphEdge],
    ) -> None:
        if not work_ids:
            return
        with connection.cursor() as cursor:
            cursor.execute(
                "select source_id, target_id, kind from work.work_relation "
                "order by created_at desc limit %s",
                (_WORK_LIMIT * 2,),
            )
            rows = cursor.fetchall()
        for source_id, target_id, kind in rows:
            source = str(source_id)
            target = str(target_id)
            if source in work_ids and target in work_ids:
                edges.append(GraphEdge(f"work:{source}", f"work:{target}", str(kind)))

    def _job_nodes(
        self,
        connection: Any,
        work_ids: set[str],
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "select id, work_item_id, step_id, kind, state, priority, attempt_count "
                "from runtime.job "
                "where state in ('ready', 'running', 'recovery-required', 'blocked') "
                "order by priority, updated_at desc limit %s",
                (_JOB_LIMIT,),
            )
            rows = cursor.fetchall()
        for row in rows:
            job_id = str(row[0])
            state = str(row[4])
            node_id = f"job:{job_id}"
            step = str(row[2]) if row[2] is not None else "adım yok"
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    kind="job",
                    label=_bounded_label(f"{row[3]} · {step} · {state} · deneme {row[6]}", 148),
                    canonical_ref=f"db:runtime.job/{job_id}",
                )
            )
            edges.append(GraphEdge("runtime:cluster:run", node_id, f"state:{state}"))
            if row[1] is not None and str(row[1]) in work_ids:
                edges.append(GraphEdge(node_id, f"work:{row[1]}", "executes-work"))

    def _model_nodes(
        self,
        connection: Any,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "select id, access_name, health_state, benchmark_state "
                "from models.model_inventory where enabled = true "
                "order by updated_at desc limit %s",
                (_MODEL_LIMIT,),
            )
            rows = cursor.fetchall()
        for inventory_id, access_name, health_state, benchmark_state in rows:
            model = str(inventory_id)
            node_id = f"model:{model}"
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    kind="model",
                    label=_bounded_label(f"{access_name} · {health_state}", 148),
                    canonical_ref=f"db:models.model_inventory/{model}",
                )
            )
            edges.append(
                GraphEdge("runtime:cluster:model", node_id, f"benchmark:{benchmark_state}")
            )

    def _knowledge_nodes(
        self,
        connection: Any,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "select s.id, s.slug, s.source_format, v.id, v.revision "
                "from knowledge.source s "
                "join knowledge.source_version v "
                "on v.realm_id = s.realm_id and v.source_id = s.id "
                "where v.state = 'active' order by v.created_at desc limit %s",
                (_KNOWLEDGE_LIMIT,),
            )
            rows = cursor.fetchall()
        for source_id, slug, source_format, version_id, revision in rows:
            source = str(source_id)
            node_id = f"knowledge:{source}"
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    kind="knowledge",
                    label=_bounded_label(f"{slug} · {source_format} · r{revision}", 148),
                    canonical_ref=f"db:knowledge.source_version/{version_id}",
                )
            )
            edges.append(GraphEdge("runtime:cluster:knowledge", node_id, "active-source"))

    def _memory_nodes(
        self,
        connection: Any,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        # Deliberately do not select memory.content.  The graph displays metadata
        # and drill-down references only.
        with connection.cursor() as cursor:
            cursor.execute(
                "select id, scope, memory_class, revision "
                "from memory.record where state = 'active' "
                "order by created_at desc limit %s",
                (_MEMORY_LIMIT,),
            )
            rows = cursor.fetchall()
        for memory_id, scope, memory_class, revision in rows:
            record = str(memory_id)
            node_id = f"memory:{record}"
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    kind="memory",
                    label=_bounded_label(f"{memory_class} · {scope} · r{revision}", 148),
                    canonical_ref=f"db:memory.record/{record}",
                )
            )
            edges.append(GraphEdge("runtime:cluster:memory", node_id, "active-memory"))

    def _scheduler_nodes(
        self,
        connection: Any,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "select id, job_name, interval_spec, state "
                "from ops.job_definition order by created_at desc limit %s",
                (_SCHEDULER_LIMIT,),
            )
            rows = cursor.fetchall()
        for definition_id, job_name, interval_spec, state in rows:
            definition = str(definition_id)
            node_id = f"scheduler:{definition}"
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    kind="scheduler",
                    label=_bounded_label(f"{job_name} · {interval_spec}", 148),
                    canonical_ref=f"db:ops.job_definition/{definition}",
                )
            )
            edges.append(GraphEdge("runtime:cluster:scheduler", node_id, f"state:{state}"))

    def _lease_nodes(
        self,
        connection: Any,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "select l.id, l.job_id, l.worker_label, j.state "
                "from runtime.lease l join runtime.job j on j.id = l.job_id "
                "order by l.heartbeat_at desc limit %s",
                (_AGENT_LIMIT,),
            )
            rows = cursor.fetchall()
        for lease_id, job_id, worker_label, state in rows:
            lease = str(lease_id)
            job = str(job_id)
            node_id = f"agent:{lease}"
            client = _client_from_label(str(worker_label))
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    kind="agent",
                    label=_bounded_label(f"{client} · {worker_label}", 148),
                    canonical_ref=f"db:runtime.lease/{lease}",
                )
            )
            edges.append(GraphEdge("runtime:cluster:run", node_id, "active-agent"))
            edges.append(GraphEdge(node_id, f"job:{job}", f"lease:{state}"))

    def _agents(self, connection: Any) -> tuple[ObservatoryAgent, ...]:
        with connection.cursor() as cursor:
            cursor.execute(
                "select l.id, l.job_id, l.worker_label, l.heartbeat_at, l.expires_at, "
                "j.work_item_id, j.step_id, j.state, j.project_id "
                "from runtime.lease l join runtime.job j on j.id = l.job_id "
                "order by l.heartbeat_at desc limit %s",
                (_AGENT_LIMIT,),
            )
            rows = cursor.fetchall()
        return tuple(
            ObservatoryAgent(
                agent_id=f"agent:{row[0]}",
                label=_bounded_label(str(row[2])),
                client=_client_from_label(str(row[2])),
                state=str(row[7]),
                canonical_ref=f"db:runtime.lease/{row[0]}",
                job_id=str(row[1]),
                work_item_id=None if row[5] is None else str(row[5]),
                step_id=(
                    None
                    if row[6] is None
                    else sanitize_observatory_label(str(row[6]), fallback="adım")
                ),
                heartbeat_at=_datetime(row[3]),
                lease_expires_at=_datetime(row[4]),
                project_id=str(row[8]),
            )
            for row in rows
        )

    def _events(self, connection: Any) -> tuple[ObservatoryEvent, ...]:
        with connection.cursor() as cursor:
            cursor.execute(
                "select id, job_id, event_type, occurred_at "
                "from runtime.execution_event order by occurred_at desc limit %s",
                (_EVENT_LIMIT,),
            )
            execution_rows = cursor.fetchall()
            cursor.execute(
                "select id, job_id, event_type, created_at "
                "from runtime.outbox_event order by created_at desc limit %s",
                (_EVENT_LIMIT // 2,),
            )
            outbox_rows = cursor.fetchall()

        events = [
            ObservatoryEvent(
                event_id=str(row[0]),
                event_type=_bounded_label(str(row[2]), 128),
                source="execution-event",
                occurred_at=_required_datetime(row[3]),
                canonical_ref=f"db:runtime.execution_event/{row[0]}",
                job_id=None if row[1] is None else str(row[1]),
            )
            for row in execution_rows
        ]
        events.extend(
            ObservatoryEvent(
                event_id=str(row[0]),
                event_type=_bounded_label(str(row[2]), 128),
                source="outbox",
                occurred_at=_required_datetime(row[3]),
                canonical_ref=f"db:runtime.outbox_event/{row[0]}",
                job_id=None if row[1] is None else str(row[1]),
            )
            for row in outbox_rows
        )
        events.sort(key=lambda item: item.occurred_at, reverse=True)
        return tuple(events[:_EVENT_LIMIT])

    def _canonical_runtime(self, connection: Any) -> CanonicalRuntimeProjection:
        """Project exact runtime identities without payload or success self-reports."""

        entities: list[CanonicalRuntimeEntity] = []
        contradictions: list[RuntimeContradiction] = []
        with connection.cursor() as cursor:
            cursor.execute(
                "select id,project_id,state,created_at from work.work_item "
                "order by updated_at desc limit %s",
                (_WORK_LIMIT,),
            )
            for work_id, project_id, state, occurred_at in cursor.fetchall():
                value = str(work_id)
                entities.append(
                    CanonicalRuntimeEntity(
                        entity_id=f"work:{value}",
                        kind="work",
                        state=str(state),
                        canonical_ref=f"db:work.work_item/{value}",
                        occurred_at=_required_datetime(occurred_at),
                        work_item_id=value,
                        project_id=str(project_id),
                    )
                )

            cursor.execute(
                "select j.id,j.project_id,j.work_item_id,j.state,j.created_at,"
                " exists(select 1 from runtime.effect_claim c"
                " where c.realm_id=j.realm_id and c.job_id=j.id),"
                " exists(select 1 from runtime.effect_claim c"
                " join runtime.effect_receipt r on r.realm_id=c.realm_id"
                " and r.claim_id=c.id and r.status in ('completed','failed')"
                " where c.realm_id=j.realm_id and c.job_id=j.id)"
                " from runtime.job j order by j.updated_at desc limit %s",
                (_JOB_LIMIT,),
            )
            for (
                job_id,
                project_id,
                work_id,
                raw_state,
                occurred_at,
                has_claim,
                has_terminal,
            ) in cursor.fetchall():
                job = str(job_id)
                work = None if work_id is None else str(work_id)
                terminal_bound = bool(has_claim and has_terminal)
                state = str(raw_state)
                if state == "completed" and not terminal_bound:
                    state = "completed-unbound"
                    contradictions.append(
                        RuntimeContradiction(
                            contradiction_id=f"job-completed-unbound:{job}",
                            kind="completed-without-terminal-receipt",
                            severity="high",
                            state=state,
                            canonical_ref=f"db:runtime.job/{job}",
                            observed_at=_required_datetime(occurred_at),
                            work_item_id=work,
                            job_id=job,
                        )
                    )
                entities.append(
                    CanonicalRuntimeEntity(
                        entity_id=f"job:{job}",
                        kind="job",
                        state=state,
                        canonical_ref=f"db:runtime.job/{job}",
                        occurred_at=_required_datetime(occurred_at),
                        parent_id=None if work is None else f"work:{work}",
                        work_item_id=work,
                        job_id=job,
                        terminal_receipt_bound=terminal_bound,
                        project_id=str(project_id),
                    )
                )

            cursor.execute(
                "select a.id,a.job_id,a.outcome,a.started_at,j.project_id,j.work_item_id "
                "from runtime.job_attempt a join runtime.job j "
                "on j.realm_id=a.realm_id and j.id=a.job_id "
                "order by a.started_at desc limit %s",
                (_JOB_LIMIT,),
            )
            for attempt_id, job_id, outcome, occurred_at, project_id, work_id in cursor.fetchall():
                attempt = str(attempt_id)
                job = str(job_id)
                work = None if work_id is None else str(work_id)
                entities.append(
                    CanonicalRuntimeEntity(
                        entity_id=f"attempt:{attempt}",
                        kind="attempt",
                        state="running" if outcome is None else str(outcome),
                        canonical_ref=f"db:runtime.job_attempt/{attempt}",
                        occurred_at=_required_datetime(occurred_at),
                        parent_id=f"job:{job}",
                        work_item_id=work,
                        job_id=job,
                        project_id=str(project_id),
                    )
                )

            cursor.execute(
                "select a.id,a.project_id,a.work_item_id,a.parent_assignment_id,"
                "a.role,a.status,a.created_at,"
                " j.id from agents.assignment a left join runtime.job j "
                "on j.realm_id=a.realm_id and j.assignment_id=a.id "
                "order by a.created_at desc limit %s",
                (_AGENT_LIMIT,),
            )
            for (
                assignment_id,
                project_id,
                work_id,
                parent_id,
                role,
                state,
                occurred_at,
                job_id,
            ) in cursor.fetchall():
                assignment = str(assignment_id)
                work = str(work_id)
                assignment_job = None if job_id is None else str(job_id)
                parent = (
                    f"assignment:{parent_id}"
                    if parent_id is not None
                    else (f"job:{assignment_job}" if assignment_job is not None else f"work:{work}")
                )
                entities.append(
                    CanonicalRuntimeEntity(
                        entity_id=f"assignment:{assignment}",
                        kind=f"agent-{role}",
                        state=str(state),
                        canonical_ref=f"db:agents.assignment/{assignment}",
                        occurred_at=_required_datetime(occurred_at),
                        parent_id=parent,
                        work_item_id=work,
                        job_id=assignment_job,
                        project_id=str(project_id),
                    )
                )

            cursor.execute(
                "select l.id,l.job_id,l.expires_at,l.heartbeat_at,"
                "j.project_id,j.work_item_id,j.state "
                "from runtime.lease l join runtime.job j "
                "on j.realm_id=l.realm_id and j.id=l.job_id "
                "order by l.heartbeat_at desc limit %s",
                (_AGENT_LIMIT,),
            )
            now = dt.datetime.now(dt.UTC)
            for (
                lease_id,
                job_id,
                expires_at,
                heartbeat_at,
                project_id,
                work_id,
                job_state,
            ) in cursor.fetchall():
                lease = str(lease_id)
                job = str(job_id)
                work = None if work_id is None else str(work_id)
                expires = _required_datetime(expires_at)
                state = "expired" if expires <= now else "live"
                if state == "expired" and str(job_state) == "running":
                    contradictions.append(
                        RuntimeContradiction(
                            contradiction_id=f"expired-lease:{lease}",
                            kind="running-job-with-expired-lease",
                            severity="critical",
                            state="recovery-required",
                            canonical_ref=f"db:runtime.lease/{lease}",
                            observed_at=expires,
                            work_item_id=work,
                            job_id=job,
                        )
                    )
                entities.append(
                    CanonicalRuntimeEntity(
                        entity_id=f"lease:{lease}",
                        kind="lease",
                        state=state,
                        canonical_ref=f"db:runtime.lease/{lease}",
                        occurred_at=_required_datetime(heartbeat_at),
                        parent_id=f"job:{job}",
                        work_item_id=work,
                        job_id=job,
                        project_id=str(project_id),
                    )
                )

            cursor.execute(
                "select c.id,c.job_id,c.claimed_at,j.project_id,j.work_item_id,"
                "r.id,r.status,r.completed_at "
                "from runtime.effect_claim c join runtime.job j "
                "on j.realm_id=c.realm_id and j.id=c.job_id "
                "left join runtime.effect_receipt r "
                "on r.realm_id=c.realm_id and r.claim_id=c.id "
                "order by c.claimed_at desc limit %s",
                (_JOB_LIMIT,),
            )
            for (
                claim_id,
                job_id,
                claimed_at,
                project_id,
                work_id,
                receipt_id,
                receipt_status,
                completed_at,
            ) in cursor.fetchall():
                claim = str(claim_id)
                job = str(job_id)
                work = None if work_id is None else str(work_id)
                receipt_bound = receipt_id is not None
                entities.append(
                    CanonicalRuntimeEntity(
                        entity_id=f"claim:{claim}",
                        kind="claim",
                        state="receipt-bound" if receipt_bound else "receiptless",
                        canonical_ref=f"db:runtime.effect_claim/{claim}",
                        occurred_at=_required_datetime(claimed_at),
                        parent_id=f"job:{job}",
                        work_item_id=work,
                        job_id=job,
                        terminal_receipt_bound=receipt_bound,
                        project_id=str(project_id),
                    )
                )
                if receipt_id is None:
                    contradictions.append(
                        RuntimeContradiction(
                            contradiction_id=f"receiptless-claim:{claim}",
                            kind="claim-without-terminal-receipt",
                            severity="high",
                            state="recovery-required",
                            canonical_ref=f"db:runtime.effect_claim/{claim}",
                            observed_at=_required_datetime(claimed_at),
                            work_item_id=work,
                            job_id=job,
                        )
                    )
                else:
                    receipt = str(receipt_id)
                    entities.append(
                        CanonicalRuntimeEntity(
                            entity_id=f"receipt:{receipt}",
                            kind="receipt",
                            state=str(receipt_status),
                            canonical_ref=f"db:runtime.effect_receipt/{receipt}",
                            occurred_at=_required_datetime(completed_at),
                            parent_id=f"claim:{claim}",
                            work_item_id=work,
                            job_id=job,
                            terminal_receipt_bound=True,
                            project_id=str(project_id),
                        )
                    )

        entities.sort(key=lambda item: (item.occurred_at, item.entity_id), reverse=True)
        contradictions.sort(
            key=lambda item: (item.observed_at, item.contradiction_id), reverse=True
        )
        truncated = (
            len(entities) > _RUNTIME_ENTITY_LIMIT or len(contradictions) > _CONTRADICTION_LIMIT
        )
        return CanonicalRuntimeProjection(
            entities=tuple(entities[:_RUNTIME_ENTITY_LIMIT]),
            contradictions=tuple(contradictions[:_CONTRADICTION_LIMIT]),
            available=True,
            detail="postgresql-exact-runtime-chain",
            truncated=truncated,
        )

    def _causal(self, connection: Any) -> CausalProjection:
        """Read a bounded FK-derived chain and delayed evidence gaps."""

        with connection.cursor() as cursor:
            cursor.execute(
                "select node_id,kind,state,occurred_at,canonical_ref,work_item_id,job_id "
                "from ops.causal_node order by occurred_at desc,node_id limit %s",
                (_CAUSAL_NODE_LIMIT,),
            )
            node_rows = cursor.fetchall()
            selected_node_ids = [str(row[0]) for row in node_rows]
            if selected_node_ids:
                cursor.execute(
                    "select source_node_id,target_node_id,kind from ops.causal_edge "
                    "where source_node_id=any(%s::text[]) and target_node_id=any(%s::text[]) "
                    "order by source_node_id,target_node_id,kind limit %s",
                    (selected_node_ids, selected_node_ids, _CAUSAL_EDGE_LIMIT),
                )
                edge_rows = cursor.fetchall()
            else:
                edge_rows = []
            cursor.execute(
                "select orphan_kind,severity,node_id,canonical_ref,work_item_id,job_id,"
                "observed_at,reason from ops.causal_orphan "
                "order by case severity when 'critical' then 0 when 'high' then 1 else 2 end,"
                "observed_at limit %s",
                (_ORPHAN_LIMIT,),
            )
            orphan_rows = cursor.fetchall()

        nodes = tuple(
            CausalNode(
                node_id=str(row[0]),
                kind=str(row[1]),
                state=str(row[2]),
                occurred_at=_required_datetime(row[3]),
                canonical_ref=str(row[4]),
                work_item_id=None if row[5] is None else str(row[5]),
                job_id=None if row[6] is None else str(row[6]),
            )
            for row in node_rows
        )
        known = {item.node_id for item in nodes}
        edges = tuple(
            CausalEdge(str(row[0]), str(row[1]), str(row[2]))
            for row in edge_rows
            if str(row[0]) in known and str(row[1]) in known
        )
        orphans = tuple(
            CausalOrphan(
                orphan_kind=str(row[0]),
                severity=str(row[1]),
                node_id=str(row[2]),
                canonical_ref=str(row[3]),
                work_item_id=None if row[4] is None else str(row[4]),
                job_id=None if row[5] is None else str(row[5]),
                observed_at=_required_datetime(row[6]),
                reason=str(row[7]),
            )
            for row in orphan_rows
        )
        material = {
            "nodes": [item.as_dict() for item in nodes],
            "edges": [item.as_dict() for item in edges],
            "orphans": [item.as_dict() for item in orphans],
        }
        return CausalProjection(
            nodes=nodes,
            edges=edges,
            orphans=orphans,
            source_digest=digest(material),
            available=True,
            detail=(
                "postgresql-canonical-correlation-truncated"
                if len(node_rows) == _CAUSAL_NODE_LIMIT or len(edge_rows) == _CAUSAL_EDGE_LIMIT
                else "postgresql-canonical-correlation"
            ),
            truncated=(
                len(node_rows) == _CAUSAL_NODE_LIMIT or len(edge_rows) == _CAUSAL_EDGE_LIMIT
            ),
        )


def _bounded_label(value: str, limit: int = 96) -> str:
    return sanitize_observatory_label(value, fallback="isimsiz", limit=limit)


def _client_from_label(label: str) -> str:
    folded = label.casefold()
    for client in ("opencode", "claude", "codex"):
        if client in folded:
            return client
    return "zekam"


def _datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    return _required_datetime(value)


def _required_datetime(value: Any) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        raise TypeError("database timestamp must be datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


def _unique_nodes(items: Iterable[GraphNode]) -> tuple[GraphNode, ...]:
    unique: dict[str, GraphNode] = {}
    for item in items:
        unique.setdefault(item.node_id, item)
    return tuple(unique.values())


def _unique_edges(items: Iterable[GraphEdge]) -> tuple[GraphEdge, ...]:
    unique: dict[tuple[str, str, str], GraphEdge] = {}
    for item in items:
        unique.setdefault((item.source, item.target, item.kind), item)
    return tuple(unique.values())
