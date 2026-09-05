"""PostgreSQL canonical agent assignment store."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.agents import (
    AgentAssignment,
    AgentInvocation,
    AssignmentRole,
    AssignmentStatus,
)
from zekam.domain.errors import NotFound, PolicyViolation


@dataclass(frozen=True, slots=True)
class AgentAssignmentRepository:
    connection: Any
    realm_id: UUID

    def get(self, assignment_id: UUID) -> AgentAssignment:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,realm_id,project_id,work_item_id,role,agent_ref,instruction_digest,"
                " context_manifest_digest,assignment_digest,status,parent_assignment_id,plan_id,"
                " step_id,risk,created_at from agents.assignment"
                " where realm_id=%s and id=%s",
                (self.realm_id, assignment_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise NotFound("Assignment bulunamadi")
            cursor.execute(
                "select resource,mode from agents.assignment_resource"
                " where realm_id=%s and assignment_id=%s order by resource,mode",
                (self.realm_id, assignment_id),
            )
            resources = cursor.fetchall()
        return AgentAssignment(
            id=UUID(str(row[0])),
            realm_id=UUID(str(row[1])),
            project_id=UUID(str(row[2])),
            work_item_id=UUID(str(row[3])),
            role=AssignmentRole(str(row[4])),
            agent_ref=str(row[5]),
            instruction_digest=str(row[6]),
            context_manifest_digest=str(row[7]),
            assignment_digest=str(row[8]),
            status=AssignmentStatus(str(row[9])),
            parent_assignment_id=None if row[10] is None else UUID(str(row[10])),
            plan_id=None if row[11] is None else UUID(str(row[11])),
            step_id=None if row[12] is None else str(row[12]),
            risk=str(row[13]),
            read_resources=tuple(str(item[0]) for item in resources if item[1] == "read"),
            write_resources=tuple(str(item[0]) for item in resources if item[1] == "write"),
            created_at=row[14],
        )

    def create(self, assignment: AgentAssignment) -> tuple[UUID, bool]:
        if assignment.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm assignment reddedildi")
        assignment.assert_digest()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into agents.assignment"
                " (id,realm_id,project_id,work_item_id,plan_id,step_id,parent_assignment_id,role,"
                " agent_ref,status,risk,instruction_digest,context_manifest_digest,"
                " assignment_digest,created_at)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (realm_id,assignment_digest) do nothing returning id",
                (
                    assignment.id,
                    assignment.realm_id,
                    assignment.project_id,
                    assignment.work_item_id,
                    assignment.plan_id,
                    assignment.step_id,
                    assignment.parent_assignment_id,
                    assignment.role.value,
                    assignment.agent_ref,
                    assignment.status.value,
                    assignment.risk,
                    assignment.instruction_digest,
                    assignment.context_manifest_digest,
                    assignment.assignment_digest,
                    assignment.created_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "select id from agents.assignment where realm_id=%s and assignment_digest=%s",
                    (self.realm_id, assignment.assignment_digest),
                )
                return UUID(str(cursor.fetchone()[0])), False
            for mode, resources in (
                ("read", assignment.read_resources),
                ("write", assignment.write_resources),
            ):
                for resource in resources:
                    cursor.execute(
                        "insert into agents.assignment_resource"
                        " (realm_id,assignment_id,resource,mode) values (%s,%s,%s,%s)",
                        (self.realm_id, assignment.id, resource, mode),
                    )
            return UUID(str(row[0])), True

    def complete_terminal_plan(self, plan_id: UUID, *, now: dt.datetime) -> int:
        """Close prior assignments only after all plan jobs are unambiguously terminal."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select count(*),count(*) filter(where state in"
                " ('ready','running','recovery-required')) from runtime.job"
                " where realm_id=%s and plan_id=%s",
                (self.realm_id, plan_id),
            )
            row = cursor.fetchone()
            if row is None or int(row[0] or 0) < 1 or int(row[1] or 0) != 0:
                raise PolicyViolation("Terminal plan assignment closure job state drift")
            cursor.execute(
                "select count(*) from runtime.claim_without_receipt claim"
                " join runtime.job job on job.realm_id=claim.realm_id and job.id=claim.job_id"
                " where claim.realm_id=%s and job.plan_id=%s",
                (self.realm_id, plan_id),
            )
            if int(cursor.fetchone()[0]) != 0:
                raise PolicyViolation("Terminal plan assignment closure receiptless claim tasiyor")
            cursor.execute(
                "update agents.assignment set status='completed',terminal_at=%s"
                " where realm_id=%s and plan_id=%s and status in ('ready','active')",
                (now, self.realm_id, plan_id),
            )
            return int(cursor.rowcount)

    def record_invocation(self, invocation: AgentInvocation) -> tuple[UUID, bool]:
        if invocation.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm invocation reddedildi")
        invocation.assert_digest()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into agents.invocation"
                " (id,realm_id,assignment_id,client_id,execution_identity,"
                " invocation_digest,created_at)"
                " values (%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (realm_id,invocation_digest) do nothing returning id",
                (
                    invocation.id,
                    invocation.realm_id,
                    invocation.assignment_id,
                    invocation.client_id,
                    invocation.execution_identity,
                    invocation.invocation_digest,
                    invocation.created_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "select id from agents.invocation where realm_id=%s and invocation_digest=%s",
                    (self.realm_id, invocation.invocation_digest),
                )
                return UUID(str(cursor.fetchone()[0])), False
            return UUID(str(row[0])), True

    def assert_verifier_gate(self, assignment_id: UUID) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select agents.verifier_gate_satisfied(%s,%s)", (self.realm_id, assignment_id)
            )
            if not bool(cursor.fetchone()[0]):
                raise PolicyViolation("Yuksek riskli assignment bagimsiz verifier ister")

    def store_result(
        self, *, assignment_id: UUID, invocation_id: UUID, envelope_digest: str
    ) -> None:
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select role from agents.assignment where realm_id=%s and id=%s",
                (self.realm_id, assignment_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise PolicyViolation("Assignment bulunamadi")
            if str(row[0]) == AssignmentRole.COORDINATOR.value:
                raise PolicyViolation("Koordinator child sonucu uretemez")
            cursor.execute(
                "insert into agents.result_receipt"
                " (realm_id,assignment_id,invocation_id,envelope_digest) values (%s,%s,%s,%s)",
                (self.realm_id, assignment_id, invocation_id, envelope_digest),
            )
