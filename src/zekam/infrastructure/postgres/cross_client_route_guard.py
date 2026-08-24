"""Cross-client handoff icin canonical current route dogrulamasi."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.context_continuity import TargetRouteBinding
from zekam.domain.errors import PolicyViolation


@dataclass(frozen=True, slots=True)
class PostgresCrossClientRouteGuard:
    connection: Any
    realm_id: UUID

    def require_current(
        self,
        decision_id: UUID,
        *,
        target_model_ref: str,
        target_client_id: str,
        project_id: str,
        at: dt.datetime | None,
    ) -> TargetRouteBinding:
        if at is not None and at.tzinfo is None:
            raise PolicyViolation("Cross-client route zamani timezone-aware olmali")
        try:
            project_uuid = UUID(project_id)
        except ValueError as exc:
            raise PolicyViolation("Cross-client route canonical project UUID ister") from exc
        with self.connection.cursor() as cursor:
            if at is None:
                cursor.execute("select clock_timestamp()")
                at = cursor.fetchone()[0]
            cursor.execute(
                "select d.evidence_digest,d.primary_model_id,"
                " least(et.expires_at,coalesce(rp.expires_at,et.expires_at)) as valid_until"
                " from models.model_route_decision d"
                " join models.execution_target_snapshot et"
                "   on et.realm_id=d.realm_id and et.id=d.execution_target_id"
                " join models.routing_role_policy rp"
                "   on rp.realm_id=d.realm_id and rp.id=d.role_policy_id"
                " join models.model_route_candidate c"
                "   on c.realm_id=d.realm_id and c.decision_id=d.id"
                "  and c.model_id=d.primary_model_id and c.disposition='primary'"
                " where d.realm_id=%s and d.id=%s and d.status='selected'"
                " and d.decided_at<=%s"
                " and d.primary_model_id=%s and et.client_id=%s"
                " and (d.project_id is null or d.project_id=%s)"
                " and et.captured_at<=%s and et.expires_at>%s"
                " and rp.effective_from<=%s and (rp.expires_at is null or rp.expires_at>%s)"
                " and d.id=(select latest.id from models.model_route_decision latest"
                "   where latest.realm_id=d.realm_id and latest.role=d.role"
                "   and latest.decided_at<=%s"
                "   and latest.target_layer=d.target_layer"
                "   and latest.project_id is not distinct from d.project_id"
                "   and latest.workload is not distinct from d.workload"
                "   and latest.technology is not distinct from d.technology"
                "   order by latest.decided_at desc,latest.id desc limit 1)"
                " group by d.evidence_digest,d.primary_model_id,et.expires_at,rp.expires_at",
                (
                    self.realm_id,
                    decision_id,
                    at,
                    target_model_ref,
                    target_client_id,
                    project_uuid,
                    at,
                    at,
                    at,
                    at,
                    at,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            raise PolicyViolation("Cross-client target route current/fresh degil")
        return TargetRouteBinding(
            decision_id=decision_id,
            evidence_digest=str(row[0]),
            target_model_ref=str(row[1]),
            valid_until=row[2],
            observed_at=at,
        )
